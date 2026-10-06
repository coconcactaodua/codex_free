use async_trait::async_trait;
use serde_json::{Value, json};

use crate::exec_sessions::SessionState;
use crate::memory::save_plan;
use crate::tool::{Tool, arg_str};
use crate::types::{AppConfig, PlanItem, PlanState, PlanStepStatus, ToolResult};

/// Parse the raw `plan` argument into typed items, mirroring the TS `parsePlan`
/// error messages verbatim.
fn parse_plan(raw: &Value) -> Result<Vec<PlanItem>, String> {
    let Some(arr) = raw.as_array() else {
        return Err("plan must be an array of { step, status } objects".to_string());
    };

    let mut out: Vec<PlanItem> = Vec::with_capacity(arr.len());
    for (index, entry) in arr.iter().enumerate() {
        if !entry.is_object() {
            return Err(format!(
                "plan[{index}] must be an object with \"step\" and \"status\""
            ));
        }
        let step = match entry.get("step").and_then(|v| v.as_str()) {
            Some(s) if !s.trim().is_empty() => s.to_string(),
            _ => return Err(format!("plan[{index}].step must be a non-empty string")),
        };
        let status = match entry
            .get("status")
            .and_then(|v| v.as_str())
            .and_then(PlanStepStatus::parse)
        {
            Some(s) => s,
            None => {
                return Err(format!(
                    "plan[{index}].status must be one of: pending, in_progress, completed"
                ));
            }
        };
        out.push(PlanItem { step, status });
    }
    Ok(out)
}

fn marker(status: PlanStepStatus) -> &'static str {
    match status {
        PlanStepStatus::Pending => "[ ]",
        PlanStepStatus::InProgress => "[~]",
        PlanStepStatus::Completed => "[x]",
    }
}

/// Codex shows the plan in its TUI. Here the tool result is the only channel back
/// to the caller, so the stored batch plan is rendered in full on each boundary
/// update. The model is instructed not to call this for individual commands.
fn render_plan(plan: &PlanState) -> String {
    let mut lines: Vec<String> = Vec::new();
    // `if (plan.explanation)` in JS is falsy for the empty string, so an empty
    // explanation is stored but not rendered.
    if let Some(explanation) = plan.explanation.as_deref()
        && !explanation.is_empty()
    {
        lines.push(explanation.to_string());
        lines.push(String::new());
    }
    for item in &plan.plan {
        lines.push(format!("{} {}", marker(item.status), item.step));
    }
    let done = plan
        .plan
        .iter()
        .filter(|i| i.status == PlanStepStatus::Completed)
        .count();
    lines.push(String::new());
    lines.push(format!("{done}/{} steps completed", plan.plan.len()));
    lines.join("\n")
}

pub struct UpdatePlan;

#[async_trait]
impl Tool for UpdatePlan {
    fn name(&self) -> &'static str {
        "update_plan"
    }

    fn description(&self) -> String {
        "Updates the batch plan.\nProvide an optional checkpoint explanation and a list of 3-7 logical batches, each with a step and status. At most one batch can be in_progress at a time.\nUse this once at the start of substantial work and once at a verified batch boundary; do not create one plan item per command. The plan is echoed back and saved so recall can resume from the latest checkpoint in a later conversation.".into()
    }

    fn input_schema(&self) -> Value {
        json!({
            "type": "object",
            "properties": {
                "explanation": { "type": "string", "description": "Optional explanation for this plan update." },
                "plan": {
                    "type": "array",
                    "description": "The list of steps",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step": { "type": "string", "description": "Task step text." },
                            "status": { "type": "string", "enum": ["pending", "in_progress", "completed"], "description": "Step status." }
                        },
                        "required": ["step", "status"],
                        "additionalProperties": false
                    }
                }
            },
            "required": ["plan"],
            "additionalProperties": false
        })
    }

    fn output_schema(&self) -> Option<Value> {
        Some(json!({
            "type": "object",
            "properties": {
                "content": { "type": "string", "description": "The stored plan rendered as a checklist" }
            }
        }))
    }

    async fn call(&self, args: Value, config: &AppConfig, session: &SessionState) -> ToolResult {
        let raw = args.get("plan").cloned().unwrap_or(Value::Null);
        let plan = match parse_plan(&raw) {
            Ok(p) => p,
            Err(msg) => return ToolResult::error(msg),
        };

        let in_progress: Vec<&PlanItem> = plan
            .iter()
            .filter(|i| i.status == PlanStepStatus::InProgress)
            .collect();
        if in_progress.len() > 1 {
            let steps = in_progress
                .iter()
                .map(|i| serde_json::to_string(&i.step).unwrap_or_default())
                .collect::<Vec<_>>()
                .join(", ");
            return ToolResult::error(format!(
                "At most one step can be in_progress at a time (got {}: {})",
                in_progress.len(),
                steps
            ));
        }

        let explanation = arg_str(&args, "explanation").map(|s| s.to_string());
        let state = PlanState { explanation, plan };

        let rendered = render_plan(&state);
        *session.plan.lock().unwrap() = Some(state.clone());

        // Best effort: a plan the model can see is worth more than a plan that
        // failed to persist, so a read-only state directory must not fail the call.
        save_plan(config, Some(state));

        ToolResult::text(rendered)
    }
}

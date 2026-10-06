use async_trait::async_trait;
use serde_json::{Value, json};

use crate::exec_sessions::SessionState;
use crate::memory::{memory_enabled, remember};
use crate::tool::{Tool, arg_str};
use crate::types::{AppConfig, ToolResult};

pub struct Remember;

#[async_trait]
impl Tool for Remember {
    fn name(&self) -> &'static str {
        "remember"
    }

    fn description(&self) -> String {
        "Save one durable note about this project or task, under a short key. Notes outlive the conversation and are handed back at the start of the next one, so use this for anything that would be expensive to rediscover: a decision and its reason, a non-obvious constraint, where something unexpected lives, what you already tried that did not work. Writing to a key that exists replaces it — prefer updating a key over adding near-duplicates. Passing an empty value removes the key, which is how you retract a note you have found to be wrong. Do not use this for things the repository already records; put lasting project conventions in AGENTS.md instead.".into()
    }

    fn input_schema(&self) -> Value {
        json!({
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Short stable identifier, e.g. \"auth-approach\" or \"why-not-esm\". Reusing a key overwrites that note."
                },
                "value": {
                    "type": "string",
                    "description": "The note itself, in a sentence or two. Empty removes the key."
                }
            },
            "required": ["key", "value"],
            "additionalProperties": false
        })
    }

    fn output_schema(&self) -> Option<Value> {
        Some(json!({
            "type": "object",
            "properties": {
                "content": { "type": "string", "description": "What was stored, or why it was rejected." }
            }
        }))
    }

    async fn call(&self, args: Value, config: &AppConfig, _session: &SessionState) -> ToolResult {
        let key = arg_str(&args, "key").unwrap_or("");
        let value = arg_str(&args, "value").unwrap_or("");

        if key.trim().is_empty() {
            return ToolResult::error("key must be a non-empty string");
        }
        if !memory_enabled(config) {
            return ToolResult::error(
                "Persistent memory is disabled on this server (memory.enabled is false), so this note was not saved. Keep it in the conversation instead.",
            );
        }

        let config = config.clone();
        let key = key.to_string();
        let value = value.to_string();
        let now = chrono::Utc::now().to_rfc3339();

        let result =
            tokio::task::spawn_blocking(move || remember(&config, &key, &value, &now)).await;
        let result = match result {
            Ok(r) => r,
            Err(e) => return ToolResult::error(e.to_string()),
        };

        if !result.ok {
            return ToolResult::error(result.message);
        }
        ToolResult::text(result.message)
    }
}

// SPDX-License-Identifier: GPL-3.0-or-later
// core/config.js - provider-agnostic constants: app identity, system prompt,
// feedback strings, tool categorisation. NOTHING in this file may reference a
// specific AI site (DOM, selectors, site names) - that lives in providers/*.
// eslint-disable-next-line no-unused-vars
const ZS = (() => {
  "use strict";

  // Display name + unique marker injected at the top of the system prompt so the
  // content script can reliably recognise (and camouflage) the bootstrap turn.
  const APP_NAME = "Codex Free Agent";
  const SYS_MARKER = "⟦ZS-SYS⟧";
  // A re-statement of the system prompt mid-session (see withSysResend in
  // core/main.js). It carries SYS_MARKER TOO - that is what drives camouflage
  // and session detection, and neither should change - plus this second marker,
  // purely so the chip can say "Reminder" instead of inheriting the bootstrap's
  // "Starting Up". Same content, different label: a re-injection is not a start.
  const RESEND_MARKER = "⟦ZS-RE⟧";
  const BATCH_EXECUTE_MARKER = "⟦ZS-BATCH-EXECUTE⟧";
  const BATCH_CHECKPOINT_MARKER = "⟦ZS-BATCH-CHECKPOINT⟧";

  // ── Tool → visual category (icon + colour theme for the chips) ─────────
  // MCP only. Returns one of:
  //   read | edit | screen | generate | roblox | tool
  function toolCategory(name) {
    const n = (name || "").includes("/") ? name.split("/").pop() : (name || "");
    if (n === "list_commands" || n === "list_tools" || n === "monolith_discover" || n === "monolith_status") return "read";
    if (n.endsWith("_query") || /^(script_read|script_search|script_grep|search_game_tree|inspect_instance|get_studio_state|get_console_output|search_creator_store|list_roblox_studios)$/.test(n))
      return "read";
    if (/^(multi_edit|insert_from_creator_store|store_image|monolith_update|monolith_reindex)$/.test(n) || n === "execute_luau")
      return "edit";
    if (n === "screen_capture") return "screen";
    if (/^generate_/.test(n)) return "generate";
    if (n === "unreal" || n.startsWith("roblox") || n.startsWith("monolith") || /studio|luau|instance|workspace/i.test(n)) return "roblox";
    return "tool";
  }

  // Feedback strings sent back to the model so it can self-correct.
  const FEEDBACK = {
    // A command-shaped reply that could not be turned into a runnable call.
    // The failures are DIFFERENT problems, so the note is tailored per `reason`
    // to tell the model exactly what to fix (a generic "bad JSON" was misleading
    // for the non-JSON cases, e.g. a missing ###LUA### opener). Falls back to the
    // generic "malformed" text for any unrecognised reason.
    parseError: (reason, toolName) => {
      // ###LUA### is execute_luau-ONLY (the parser always maps a bare ###LUA###
      // block to execute_luau). So only suggest it when the broken command IS
      // execute_luau, or when we could not tell which command it was. For a KNOWN
      // other command (e.g. execute_blender_code) the ###LUA### hint is wrong and
      // misleading - a model that followed it would ship its code to the wrong MCP
      // - so drop it and keep the JSON-only guidance.
      const otherCmd = toolName && toolName !== "command" && toolName !== "execute_luau";
      const luaMalformed = otherCmd ? "" : " (or use the ###LUA### / ###END_LUA### block for execute_luau)";
      const luaUnclosed = otherCmd ? "" : " (or a complete ###LUA### ... ###END_LUA### block for execute_luau)";
      const objAlt = otherCmd ? "" : " (or ###...### block)";
      const notes = {
        malformed:
      "ERROR: a Codex Free batch command was detected in your reply but its JSON could not be parsed. " +
          'Rewrite it as a single valid JSON object in plain text, exactly like {"command": "name", "params": {...}}' +
          luaMalformed + ". You may add a short note around it. " +
          "Please retry.",
        unclosed:
          "ERROR: your Codex Free command was cut off before it finished - the JSON object" +
          objAlt + " never closed, so it could not run. Rewrite the WHOLE command in one " +
          'piece as valid JSON, exactly like {"command": "name", "params": {...}}' +
          luaUnclosed + ". Please retry.",
        luaOpener:
          "ERROR: you wrote the closing ###END_LUA### marker but not the opening ###LUA### marker, " +
          "so the Luau block was not detected and did not run. Put ###LUA### immediately BEFORE your " +
          "code and ###END_LUA### after it. Please retry.",
        envelope:
          "ERROR: you wrote a command's parameters as a bare JSON object, but without the required " +
          "envelope, so it was not recognised as a command. Wrap them like " +
          '{"command": "name", "params": { ...your parameters... }} - the parameter keys go INSIDE ' +
          '"params". Please retry.',
        // The model named a REAL tool but under the wrong key - it wrote the call
        // the way a function-calling API would (e.g. {"toolName": "get_studio_state",
        // "studio_id": "..."}) instead of ZeroScript's envelope. Seen live on
        // ChatGPT in a long session. Naming the wrong keys explicitly matters: a
        // generic "bad JSON" note made the model rewrite the SAME shape.
        toolKey:
          "ERROR: you used the wrong key to name the command, so it was not recognised and did not " +
          'run. The key must be exactly "command" - not "toolName", "tool", "name", "function" or ' +
          '"action" - and every argument goes INSIDE "params", like ' +
          '{"command": "name", "params": { ...your parameters... }}. Please retry.',
        dsml:
          "ERROR: you wrote that call in your own internal tool-call markup (DSML invoke/parameter tags). " +
          "The Codex Free agent cannot read that format, so the command did not run. Never use those tags here. " +
          'Write the call as one plain-text JSON object instead, exactly like {"command": "name", "params": { ... }}. ' +
          "Please retry.",
      };
      return notes[reason] || notes.malformed;
    },
    multiTool: (names) =>
      "ERROR: You wrote multiple commands in one reply. Write ONE command at a " +
      "time and wait for its result before the next. You tried: " +
      names.join(", ") +
      ". Start over and write only the first command you need.",
    unknownTool: (name, valid) =>
      `ERROR: unknown command "${name}". It does not exist. Valid commands are: ` +
      valid.join(", ") +
      ". Use an exact name and parameter keys from the system prompt.",
    studioOffline:
      "ERROR: the selected MCP server is not reachable via the local Codex Free bridge. " +
      "Inspect list_mcp_servers, identify the offline server, and tell the user what must be started.",
    // The page outlived the extension build it was running (reload / auto-update
    // / disable+enable). Nothing here can recover it - only a page reload can -
    // so the model must NOT be told the bridge is down and must NOT retry, or it
    // burns the whole conversation re-issuing commands that can never run. See
    // isContextInvalidated in core/main.js.
    staleExtension:
      "ERROR: the Codex Free Agent extension was reloaded or updated while this page was open, so this " +
      "tab is running a version of it that no longer exists and NO command can reach the user's " +
        "machine from here. The bridge and MCP servers are NOT the problem - do not tell the user " +
      "to check them, and do not retry the command, because every retry will fail the same way. " +
      "Tell the user in one short sentence to RELOAD THIS PAGE (F5), then stop and wait.",
    bridgeOffline:
      "ERROR: the local Codex Free agent bridge is unreachable, so no command could run. " +
      "This is an environment problem on the user's machine (the bridge is not " +
      "running, or an MCP child is offline), NOT your mistake. Tell the user in " +
      "one short sentence that the bridge or MCP server is offline, then stop " +
      "sending commands until they confirm it is back.",
    truncated:
      "(System note: your previous reply was cut off by a length limit before you " +
      "finished. Continue from exactly where you stopped. Do NOT restart and do " +
      "NOT repeat what you already wrote.)",
  };

  const BT = "```";

  function compactTools(tools) {
    return (tools || [])
      .map((t) => {
        const name = t.name || "?";
        const desc = (t.description || "").split("\n")[0].trim();
        const props = (t.inputSchema && t.inputSchema.properties) || {};
        const args = Object.keys(props).join(", ");
        return `  ${name}(${args}) - ${desc}`;
      })
      .join("\n");
  }

  // Each bridge server may carry a toolkit profile from config.json. Keep the
  // profile in the live server catalogue so the model can select the right
  // operating rules instead of treating every MCP as interchangeable.
  function toolkitText(server) {
    const tk = server && server.toolkit;
    if (!tk || typeof tk !== "object") return "";
    const lines = [`Toolkit: ${tk.name || "MCP Toolkit"}`];
    if (tk.purpose) lines.push(`Purpose: ${tk.purpose}`);
    if (Array.isArray(tk.rules)) lines.push(...tk.rules.map((r) => `- ${r}`));
    if (Array.isArray(tk.skills)) {
      const enabled = tk.skills.filter((s) => s && s.enabled !== false);
      const disabled = tk.skills.filter((s) => s && s.enabled === false);
      if (enabled.length) {
        lines.push(`Loaded skills: ${enabled.map((s) => s.name || s.id).join(", ")}`);
        enabled.forEach((s) => {
          lines.push(`Skill ${s.name || s.id}: ${s.instructions || s.description || "Use this skill when its capability matches the task."}`);
        });
      } else {
        lines.push("Loaded skills: none");
      }
      if (disabled.length) lines.push(`Disabled skills: ${disabled.map((s) => s.name || s.id).join(", ")}`);
    }
    return lines.join("\n");
  }

  // ── System prompt ─────────────────────────────────────────────────────────
  // ONE unified prompt sent to every AI on the first turn. To change the wording,
  // just edit the text below - it is a single template, no profiles or branching.
  // `${siteName}` is filled in with the AI's display name (e.g. "DeepSeek").
  // `${toolsString}` is filled in with the live command list.
  //
  // `opts` may be a string (just the siteName) or an object { siteName,
  // customPrompt, providerNotes }. `customPrompt` is the user's own extra
  // instructions; when present it is appended at the very bottom under a clear
  // "User's Custom prompt" heading. It NEVER edits the prompt above - it only
  // adds a layer below it.
  //
  // `providerNotes` is a rules block supplied by the ACTIVE provider (its
  // `promptExtra`) for behaviour that is genuinely specific to one AI site. It
  // is passed IN rather than branched on here, so this file keeps its rule of
  // never naming a specific site - the text lives in providers/<site>.js and
  // every other provider is untouched by definition.
  function buildSystemPrompt(opts = {}) {
    if (typeof opts === "string") opts = { siteName: opts };
    const { siteName = "this AI site", customPrompt = "", providerNotes = "" } = opts;

    const prompt = `CONTEXT: a local Codex Free browser agent is running in this page. It watches structured command blocks in your replies, calls the enabled MCP servers, and feeds a batch result back to you. The bridge can expose several MCP servers at the same time. Never assume a server or tool exists until you inspect the live catalogue.

This agent is batch-oriented. Do not emit one command, wait for a result, and repeat that pattern for every small action. First understand the user's whole request, split it into bounded batches, and finish one batch before moving to the next. A batch normally contains 3-7 related task items; for a very large checklist use a configured batch size of about five items. Each batch must research, execute, verify, and leave a checkpoint.

MANDATORY BATCH PROTOCOL:
1. In your first reply, emit a compact plan in this exact marker:
<BATCH_PLAN>{"batch_size":5,"batches":[{"id":1,"goal":"...","items":["..."]}]}</BATCH_PLAN>
2. Then emit the commands required for the CURRENT batch. You may emit several command blocks in one reply. Keep each block complete and use the exact live tool name and argument schema.
3. After the bridge executes the whole batch, you will receive one consolidated batch result. Reply with a checkpoint in this exact marker:
<BATCH_CHECKPOINT>{"batch":1,"status":"completed","summary":"...","verified":["..."],"next":"...","blockers":[]}</BATCH_CHECKPOINT>
4. Do not start the next batch in the same checkpoint reply. The agent will automatically send the next batch prompt using your checkpoint and results. Continue until every planned batch is complete.
5. If a command fails, diagnose it, retry only when the operation is safe and idempotent, and record the failure or recovery in the checkpoint. Never blindly repeat a write, migration, build, or process launch after an ambiguous timeout.

Formatting:
- Every executable command must be inside its own ###MCP_TOOL### ... ###END_MCP_TOOL### block. A command object is {"command":"exact_tool_name","params":{...}}. You may include several such blocks for one batch.
- Progress prose is allowed and remains visible to the user. Use short factual lines such as "Đã tóm tắt kế hoạch..." or "Đã xác định nguyên nhân...".
- The agent keeps command cards and their complete arguments/results expandable. Do not hide the work in a prose-only answer.
- Never invent tool names or parameters. Call list_mcp_servers first, then list_commands for the relevant server when the schema is not already known.
- Before file, shell, or project-specific work, call list_projects and set_project_root with the one unambiguous project selector. Keep that binding for all later batches.
- Ask the user only for a real decision or confirmation. Do not ask them to type continue after a successful batch.
- Final output is plain text after all batches are verified.

Your first action must be exactly one command block to inspect the available MCP servers:
###MCP_TOOL###
{"command":"list_mcp_servers","params":{}}
###END_MCP_TOOL###`;

    const siteRules = providerNotes.trim()
      ? `\n\n━━━ ADDITIONAL RULES FOR THIS SITE ━━━\n${providerNotes.trim()}`
      : "";

    const extra = customPrompt.trim()
      ? `\n\n━━━ USER'S CUSTOM PROMPT ━━━\n${customPrompt.trim()}`
      : "";

    return `${SYS_MARKER}\n${prompt}${siteRules}${extra}`;
  }

  const TOOL_NOTES = {
    unreal:
      "Gateway MCP server. Use operation search, describe, or execute; inspect the live schema first.",
    monolith_discover:
      "Lists available tool namespaces and actions. Pass namespace or category to filter.",
    describe_query:
      "Use action 'action_schema' with target_namespace and target_action to inspect exact parameter requirements.",
    source_query:
      "Query or search C++/Unreal source files in the project workspace.",
    blueprint_query:
      "Query, search, or inspect Blueprint graphs, nodes, and functions.",
  };

  function toolsReminder(tools) {
    const toolsString =
      "  list_mcp_servers() - list enabled MCP servers and their health\n" +
      "  list_commands(server) - list commands for one enabled MCP server\n" +
      compactTools(tools);
    return (
      "\n\n────────────────────────────────\n" +
      "(System note from Codex Free - automatic REMINDER. Do NOT reply directly; keep in mind for your next command.)\n" +
      "Reminder of connected MCP commands:\n" +
      toolsString
    );
  }

  function memoryNudge() {
    return "";
  }

  return {
    APP_NAME,
    SYS_MARKER,
    RESEND_MARKER,
    BATCH_EXECUTE_MARKER,
    BATCH_CHECKPOINT_MARKER,
    FEEDBACK,
    toolCategory,
    buildSystemPrompt,
    compactTools,
    toolkitText,
    toolsReminder,
    memoryNudge,
    TOOL_NOTES,
  };
})();

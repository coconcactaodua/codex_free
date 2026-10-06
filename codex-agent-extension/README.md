# Codex Free Batch Agent

This browser extension turns a supported ChatGPT conversation into a resumable
Codex Free batch agent. It reads structured MCP blocks from model replies,
calls the local bridge, groups related calls into one bounded batch, sends one
consolidated result back, and starts the next batch automatically.

## What the user sees

- The model first emits a `BATCH_PLAN` with a bounded list of batches.
- Each batch is one model turn containing several related MCP calls, capped at
  twelve calls and normally planned at five task items.
- The extension renders an expandable batch card. Expand it to see every MCP
  call; expand a call row again to see its exact arguments and complete result.
- A checkpoint marker is required after each batch. The checkpoint is included
  in the next prompt, so the conversation continues without manual copy/paste.
- Only safe, idempotent operations may be retried automatically. Ambiguous
  writes, builds, migrations, and process launches are left for diagnosis.

## Setup on Windows

1. Build the release binary in `D:\codex-free-master`:

   ```powershell
   cargo build --release
   ```

2. Start the browser bridge:

   ```text
   D:\codex-free-master\start-codex-agent-bridge.bat
   ```

   It listens on `ws://127.0.0.1:17613` and starts the configured native
   Codex Free stdio MCP child. Additional MCP servers can be added to
   `codex-agent-bridge/config.json`; they are launched in parallel and their
   tools are kept in one live catalogue.

3. In Chrome or Edge, open `chrome://extensions` or `edge://extensions`, enable
   **Developer mode**, choose **Load unpacked**, and select:

   ```text
   D:\codex-free-master\codex-agent-extension
   ```

4. Open a **new** conversation at `https://chatgpt.com`, click **Start Codex
   Free agent**, and send the complete task. Reload the page after changing the
   extension files.

The existing `start-chatgpt-tunnel.bat` remains the native MCP tunnel for
clients that connect directly to the Rust HTTP server. The browser batch agent
uses the separate WebSocket bridge because an MCP server cannot create a new
ChatGPT user turn by itself.

## Extension files

```text
core/config.js       batch system prompt, markers, feedback, tool categories
core/parser.js       parses one or many ###MCP_TOOL### blocks
core/main.js         batch session, execution, checkpoints, nested expandable UI
providers/chatgpt.js ChatGPT DOM and composer adapter
background.js        WebSocket bridge client and reconnection logic
overlay.css          batch and nested command card styles
```

The other provider adapters are retained for compatibility, but ChatGPT is the
primary supported route for this Codex Free integration.

## Smoke tests

Run from this directory:

```powershell
node test-parser.js
node test-chatgpt.js
node --check core/config.js
node --check core/parser.js
node --check core/main.js
```

The local bridge source is derived from the ZeroScript bridge and carries its
GPL-3.0-or-later notice in
`D:\codex-free-master\codex-agent-bridge\ZEROSCRIPT-LICENSE`.

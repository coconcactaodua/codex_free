$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '.')).Path
$template = Get-Content (Join-Path $root 'codex.config.template.json') -Raw | ConvertFrom-Json
$keyPath = Join-Path $root 'openai-runtime-api-key.txt'
if (-not (Test-Path $keyPath)) { throw "Missing $keyPath" }
$template.openaiTunnel.apiKeyRef = "file:$keyPath"
$npx = Get-Command npx.cmd -ErrorAction SilentlyContinue
if (-not $npx) { throw 'Node.js/npm is required. Install Node.js LTS so npx.cmd is available.' }
$template.mcpServers.'ue-mcp'.command = $npx.Source
$ue = $env:UE_PROJECT_PATH
if ([string]::IsNullOrWhiteSpace($ue)) {
  $template.mcpServers.'ue-mcp'.disabled = $true
} else {
  $ue = (Resolve-Path $ue).Path
  $template.mcpServers.'ue-mcp'.args[2] = $ue.Replace('\','/')
  $template.mcpServers.'ue-mcp'.cwd = (Split-Path $ue -Parent).Replace('\','/')
  $template.mcpServers.'ue-mcp'.disabled = $false
}
$runtime = Join-Path $root 'codex.config.runtime.json'
$template | ConvertTo-Json -Depth 30 | Set-Content $runtime -Encoding UTF8
$binary = Join-Path $root 'codex-free.exe'
if (-not (Test-Path $binary)) { throw "Missing $binary" }
& $binary --config $runtime --work-dir $root

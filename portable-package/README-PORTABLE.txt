CODEX FREE PORTABLE

1. Install Node.js LTS on the target machine (required for Unreal MCP).
2. Put your Unreal project path in UE_PROJECT_PATH, for example:
   set UE_PROJECT_PATH=D:\UE5.8.2Project\DeltaForce\DeltaForce.uproject
3. Copy openai-runtime-api-key.txt.example to openai-runtime-api-key.txt and
   paste your OpenAI runtime API key into that local file.
4. Run start-portable.bat.

Without UE_PROJECT_PATH, Codex Free starts with native tools and tunnel only.
The runtime key file is intentionally excluded from Git. Never commit or share it.

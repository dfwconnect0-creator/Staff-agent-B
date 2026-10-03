STATUS: PASS

CONFIG PATH:
/home/bladina/.config/opencode/opencode.jsonc

CHANGES:
- Added local Ollama provider; main 9b-16k, small 4b-16k; context 16384/output 4096.
- Backup: opencode.jsonc.bak-20261001-173017. Unrelated configuration unchanged; no model downloads.

TEST:
Ollama API responded. OpenCode sees both models.
`opencode --pure run -m ollama/qwen3.5:9b-16k --title "Local Ollama smoke test" --format json "Reply exactly: LOCAL OPENCODE OLLAMA OK"`
Returned LOCAL OPENCODE OLLAMA OK; exit 0 in 7.6 seconds. Empty temporary directory; MCP servers/plugins/tools disabled for the test only. One test; no repairs.

OLLAMA PS:
qwen3.5:9b-16k (6e35f39f03ff); context 16384; 100% GPU; 5.9 GB.

NEXT STEP:
Direct OpenCode → Ollama path is verified. Headroom can now be tested separately.

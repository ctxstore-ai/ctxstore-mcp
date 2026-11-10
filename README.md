# ctxstore-mcp

**This package is retired.** ctxstore is a hosted MCP server; there is nothing
to install.

Connect any MCP client that speaks HTTP to:

```
https://mcp.ctxstore.ai/mcp
```

Claude Code, at project scope so one project's setup never changes another's:

```bash
claude mcp add --transport http --scope project ctxstore https://mcp.ctxstore.ai/mcp
```

No key is needed up front. On the first use the agent calls `setup_account`
with your email, you read a six-digit code out of your inbox, and the agent is
signed in. Never paste a key into a chat.

Clients that only speak stdio can use the standard bridge:

```json
{ "mcpServers": { "ctxstore": { "command": "npx", "args": ["-y", "mcp-remote", "https://mcp.ctxstore.ai/mcp"] } } }
```

Per-client instructions, Cursor and Codex and claude.ai included:
<https://ctxstore.ai/for-agents>.

The `ctxstore-mcp` release on PyPI (1.0.0) predates the hosted server and no
longer works; it is yanked. Do not install it.

<p align="center">
  <img src="https://fiveclaw.xyz/logo.png" height="90" alt="FiveClaw" />
</p>

<p align="center">
<pre>
███████╗██╗██╗   ██╗███████╗ ██████╗██╗      █████╗ ██╗    ██╗
██╔════╝██║██║   ██║██╔════╝██╔════╝██║     ██╔══██╗██║    ██║
█████╗  ██║██║   ██║█████╗  ██║     ██║     ███████║██║ █╗ ██║
██╔══╝  ██║╚██╗ ██╔╝██╔══╝  ██║     ██║     ██╔══██║██║███╗██║
██║     ██║ ╚████╔╝ ███████╗╚██████╗███████╗██║  ██║╚███╔███╔╝
╚═╝     ╚═╝  ╚═══╝  ╚══════╝ ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝
</pre>
</p>

<h3 align="center">The local AI bridge between your FiveM server and your IDE.</h3>

<p align="center">
  <img src="https://img.shields.io/badge/tested%20on-Linux-brightgreen" alt="Tested on Linux" />
  <img src="https://img.shields.io/badge/windows-compatibility%20WIP-orange" alt="Windows WIP" />
</p>

<p align="center">
  <a href="https://pypi.org/project/fiveclaw-agent/"><img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+" /></a>
  <a href="https://github.com/fiveclaw/fiveclaw-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License: MIT" /></a>

</p>

<p align="center">
  <a href="https://fiveclaw.xyz">Website</a> · <a href="https://fiveclaw.xyz/dashboard/download">Setup Guide</a> · <a href="https://fiveclaw.xyz/pricing">Pricing</a>
</p>

---

## What is this?

`fiveclaw-agent` is a local [MCP](https://modelcontextprotocol.io) server that runs on your machine alongside your FiveM server. It gives your AI client (Claude, Cursor, Windsurf, etc.) direct access to your server's files, logs, MySQL database, and txAdmin — and connects it to the FiveClaw cloud platform for FiveM-specific AI intelligence.

**Install once. Works everywhere you code.**

---

## Quick Install

```bash
pip install fiveclaw-agent

# With SSH deployment support
pip install fiveclaw-agent[ssh]
```

You'll need a [FiveClaw account](https://fiveclaw.xyz) and an API key from your [dashboard](https://fiveclaw.xyz/dashboard/keys).

---

## How it works

```
  ┌─────────────────────────────┐
  │   Your IDE / AI Client      │  Claude Code · Cursor · Windsurf
  │   (MCP-compatible)          │  Kilo Code · Gemini CLI
  └──────────┬──────────────────┘
             │  stdio / MCP protocol
             ▼
  ┌─────────────────────────────┐
  │      fiveclaw-agent         │  ← runs locally on your machine
  │                             │
  │  ├─ 📁 Resource map + search│
  │  ├─ 🗃  MySQL queries        │
  │  ├─ 🖥  txAdmin control      │
  │  ├─ 📋 Log reader           │
  │  ├─ 🚀 SSH deploy           │
  │  └─ 🧠 Persistent memory   │
  └──────────┬──────────────────┘
             │  HTTPS · your API key
             ▼
  ┌─────────────────────────────┐
  │   FiveClaw Platform         │
  │                             │
  │  ├─ fivem-mcp  (all plans)  │  6,400+ natives · ESX · QBCore · ox
  │  └─ ai-fivem-dev-mcp (Pro+) │  analysis · security · testing
  └─────────────────────────────┘
```

Your credentials never leave your machine. The agent only forwards tool requests to FiveClaw — it never uploads your code or files.

---

## Local Tools

These run entirely on your machine. No API key needed.

| Tool | What it does |
|------|-------------|
| 📂 `repomap_generate` | Build a map of all resources in your server |
| 🔍 `tool_search` | Search Lua/JS files across your resources |
| ✅ `tool_syntax_check` | Check Lua syntax without running the server |
| 📋 `read_latest_logs` | Tail FXServer, txAdmin, and resource logs |
| 🗃 `tool_mysql_query` | Run queries against your FiveM MySQL database |
| 🖥 `tool_server_control` | Start, stop, restart the FXServer via txAdmin or custom panel |
| 🔌 `tool_resource_control` | Start/stop/restart individual resources via txAdmin or custom panel |
| 📡 `tool_server_console` | Send console commands via txAdmin or custom panel |
| 🚀 `deploy_resource` | SSH-deploy a resource directly to production |
| 🧠 `context_remember` | Store persistent notes across AI sessions |

---

## Cloud Tools

Powered by FiveClaw. Requires an API key from [fiveclaw.xyz](https://fiveclaw.xyz).

### fivem-mcp — included on all plans (even free)

> FiveM intelligence. 6,400+ native functions, framework docs, best practices, live CFX docs.

| | |
|---|---|
| 📖 Native docs | Full reference for all 6,400+ FiveM/GTA natives with examples |
| 🏗 Framework docs | ESX, QBCore, ox_lib, ox_core — guides, functions, patterns |
| 💡 Best practices | Lua performance, sync patterns, common pitfalls |
| 🔎 Anti-pattern guide | What not to do and why — with fixes |
| ⚠️ Error solutions | Database of common FiveM errors with step-by-step fixes |
| 🌐 Live CFX docs | Fetch live documentation directly from CFX |

### ai-fivem-dev-mcp — Pro + Enterprise

> Deep code intelligence for your actual server resources.

| | |
|---|---|
| 🏥 Resource health | Validate manifests, exports, load order |
| 🛡 Security scanner | Detect injection, auth bypass, and logic vulnerabilities |
| 🔗 Export contracts | Validate that imports match their exported signatures |
| 🎯 Event tracer | Trace any event from trigger to handler across resources |
| 🧪 Test engine | Unit tests, event tests, database tests, coverage reports |
| 📐 Pattern library | Scaffold new resources from reusable team templates |
| 🔍 Duplicate detector | Find copy-pasted code blocks across your codebase |
| 📦 Dependency graph | Visualize resource dependencies (Enterprise) |
| 👥 Team patterns | Shared scaffolds across your whole team (Enterprise) |

---

## Supported AI Clients

| Client | Config file |
|--------|------------|
| **Claude Code** | Project: `.mcp.json` · Global: `~/.claude.json` |
| **Cursor** | Project: `.cursor/mcp.json` · Global: `~/.cursor/mcp.json` |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` |
| **Kilo Code** | Project: `.kilocode/mcp.json` · Global: extension settings |
| **Gemini CLI** | `~/.gemini/settings.json` — merge `mcpServers` key |

The interactive setup wizard at [fiveclaw.xyz/dashboard/download](https://fiveclaw.xyz/dashboard/download) generates your config automatically.

---

## Manual Config

Add to your AI client's MCP config. Only `FIVECLAW_API_KEY` is required — remove any lines you don't use.

```json
{
  "mcpServers": {
    "fiveclaw": {
      "command": "fiveclaw",
      "env": {
        "FIVECLAW_API_KEY":    "fc_live_YOUR_API_KEY_HERE",
        "FIVEM_PROJECT_ROOT":  "/path/to/your/fivem-server",
        "FIVEM_RESOURCES_DIR": "/path/to/your/fivem-server/resources",

        "TXADMIN_URL":         "http://localhost:40120",
        "TXADMIN_USER":        "admin",
        "TXADMIN_PASS":        "YOUR_TXADMIN_PASSWORD",

        "MYSQL_HOST":          "127.0.0.1",
        "MYSQL_USER":          "root",
        "MYSQL_PASSWORD":      "YOUR_MYSQL_PASSWORD",
        "MYSQL_DATABASE":      "fivem",
        "MYSQL_EXTRA_DBS":     "{\"db2\":{\"host\":\"127.0.0.1\",\"port\":3306,\"user\":\"root\",\"password\":\"pass\",\"database\":\"db2\"}}",

        "FIVEM_SSH_HOST":      "YOUR_VPS_IP",
        "FIVEM_SSH_USER":      "root",
        "FIVEM_SSH_KEY":       "~/.ssh/id_rsa"
      }
    }
  }
}
```

### Custom Control Panel (alternative to txAdmin)

If you run a custom admin panel instead of txAdmin, set `ADMIN_PANEL_TYPE=custom` and configure all four endpoints explicitly:

```json
{
  "mcpServers": {
    "fiveclaw": {
      "command": "fiveclaw",
      "env": {
        "FIVECLAW_API_KEY":              "fc_live_YOUR_API_KEY_HERE",
        "FIVEM_PROJECT_ROOT":            "/path/to/your/fivem-server",
        "ADMIN_PANEL_TYPE":              "custom",
        "ADMIN_PANEL_URL":               "http://localhost:30121",
        "ADMIN_PANEL_STATUS_ENDPOINT":   "/api/server/status",
        "ADMIN_PANEL_START_ENDPOINT":    "/api/server-control/start",
        "ADMIN_PANEL_STOP_ENDPOINT":     "/api/server-control/stop",
        "ADMIN_PANEL_COMMAND_ENDPOINT":  "/api/server-control/command",
        "FIVEM_LOGS_DIR":                "/path/to/logs"
      }
    }
  }
}
```

> **Note:** `FIVEM_LOGS_DIR` is required when using `ADMIN_PANEL_TYPE=custom`. With txAdmin (the default), logs are auto-detected from `FIVEM_PROJECT_ROOT/logs`.

Restart your AI client after saving.

### Verify it's working

Ask your AI: *"Run mcp_health to check my FiveClaw connection."*

---

## Platform Support

| Environment | Status |
|-------------|--------|
| **Linux** (local agent + Linux FiveM server) | ✅ Fully tested |
| **macOS** (local agent) | ✅ Should work — untested |
| **Windows** (local agent or Windows FiveM server) | 🚧 Work in progress |

> **Note:** Development and testing has been done entirely on Linux (local machine + remote server). Windows support is being actively worked on — core functionality should work, but edge cases around SSH deployment and MySQL tooling on Windows may need ironing out. If you hit an issue on Windows, [open an issue](https://github.com/fiveclaw/fiveclaw-agent/issues).

## Requirements

- Python 3.10+
- A [FiveClaw account](https://fiveclaw.xyz) with an active subscription
- API key from [fiveclaw.xyz/dashboard/keys](https://fiveclaw.xyz/dashboard/keys)

---

## License

MIT — free to use, fork, and modify.  
The `fiveclaw-agent` itself is open source. The cloud tools (fivem-mcp, ai-fivem-dev-mcp) are proprietary services accessed via API key.

---

<p align="center">
  Built by <a href="https://fiveclaw.xyz">FiveClaw</a> · <a href="https://fiveclaw.xyz/pricing">Get started free</a>
</p>

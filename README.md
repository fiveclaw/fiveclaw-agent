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
  <img src="https://img.shields.io/badge/linux-supported-brightgreen" alt="Linux supported" />
  <img src="https://img.shields.io/badge/windows-supported-blue" alt="Windows supported" />
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="License: MIT" />
</p>

<p align="center">
  <a href="https://fiveclaw.xyz">Website</a> · <a href="https://fiveclaw.xyz/dashboard/download">Setup Guide</a> · <a href="https://fiveclaw.xyz/pricing">Pricing</a>
</p>

---

## What is this?

`fiveclaw-agent` is a local [MCP](https://modelcontextprotocol.io) server that runs on your machine alongside your FiveM server. It gives your AI client (Claude Code, Cursor, Windsurf, etc.) direct access to your server's files, logs, MySQL database, SSH, and txAdmin — and connects it to the FiveClaw cloud platform for FiveM-specific AI intelligence.

**Install once. Works everywhere you code.**

---

## Quick Install

```bash
pip install fiveclaw-agent
```

You'll need a [FiveClaw account](https://fiveclaw.xyz) and an API key from your [dashboard](https://fiveclaw.xyz/dashboard/keys).

---

## Configuration

The easiest way to get your MCP config is the **interactive setup wizard** at:

### 👉 [fiveclaw.xyz/dashboard/download](https://fiveclaw.xyz/dashboard/download)

It generates the exact JSON for your AI client (Claude Code, Cursor, Windsurf, Kilo Code, Gemini CLI) based on your OS, server details, and plan — including Windows-specific settings, SSH, MySQL, txAdmin, and custom panel options.

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
  │  ├─ 🖥  Server control       │
  │  ├─ 📋 Log reader           │
  │  ├─ 🔑 SSH tools            │
  │  ├─ 🚀 Deploy               │
  │  └─ 🧠 Persistent memory    │
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

These run entirely on your machine.

| Tool | What it does |
|------|-------------|
| 📂 `repomap_generate` | Build a map of all resources in your server |
| 🔍 `tool_search` | Search Lua/JS files across your resources |
| ✅ `tool_syntax_check` | Check Lua syntax (embedded LuaJIT — no external binary needed) |
| 📋 `read_latest_logs` | Tail FXServer and resource logs |
| 🗃 `tool_mysql_query` | Run queries against your FiveM MySQL database |
| 🖥 `tool_server_control` | Start, stop, restart the FXServer via txAdmin or custom panel |
| 🔌 `tool_resource_control` | Start/stop/restart individual resources |
| 📡 `tool_server_console` | Send console commands |
| 🔑 `tool_ssh_run/ls/read/write` | Full SSH access to your remote server |
| 🚀 `deploy_resource` | Deploy a resource directly to production |
| 🧠 `context_remember` | Store persistent notes across AI sessions |
| ℹ️ `tool_platform_info` | Show configured OS, paths, and enabled services |

---

## Cloud Tools

Powered by FiveClaw. Requires an API key from [fiveclaw.xyz](https://fiveclaw.xyz).

### fivem-mcp — included on all plans

| | |
|---|---|
| 📖 Native docs | Full reference for all 6,400+ FiveM/GTA natives with examples |
| 🏗 Framework docs | ESX, QBCore, ox_lib, ox_core — guides, functions, patterns |
| 💡 Best practices | Lua performance, sync patterns, common pitfalls |
| ⚠️ Error solutions | Database of common FiveM errors with step-by-step fixes |
| 🌐 Live CFX docs | Fetch live documentation directly from CFX |

### ai-fivem-dev-mcp — Pro + Enterprise

| | |
|---|---|
| 🏥 Resource health | Validate manifests, exports, load order |
| 🛡 Security scanner | Detect injection, auth bypass, and logic vulnerabilities |
| 🎯 Event tracer | Trace any event from trigger to handler across resources |
| 🧪 Test engine | Unit tests, event tests, database tests, coverage reports |
| 📐 Pattern library | Scaffold new resources from reusable team templates |
| 🔍 Duplicate detector | Find copy-pasted code across your codebase |

---

## Supported AI Clients

| Client | Config file |
|--------|------------|
| **Claude Code** | Project: `.mcp.json` · Global: `~/.claude.json` |
| **Cursor** | Project: `.cursor/mcp.json` · Global: `~/.cursor/mcp.json` |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` |
| **Kilo Code** | Project: `.kilocode/mcp.json` · Global: extension settings |
| **Gemini CLI** | `~/.gemini/settings.json` — merge `mcpServers` key |

---

## Platform Support

| Environment | Status |
|-------------|--------|
| **Linux** | ✅ Fully supported |
| **macOS** | ✅ Fully supported |
| **Windows** | ✅ Fully supported |

**Windows note:** Use `python -u -m fiveclaw_agent` as the command (not the `fiveclaw` entry point) to avoid pipe-buffering issues. The setup wizard handles this automatically.

---

## Requirements

- Python 3.10+
- A [FiveClaw account](https://fiveclaw.xyz) with an API key

---

## License

MIT — free to use, fork, and modify.  
The `fiveclaw-agent` itself is open source. The cloud tools (fivem-mcp, ai-fivem-dev-mcp) are proprietary services accessed via API key.

---

<p align="center">
  Built by <a href="https://fiveclaw.xyz">FiveClaw</a> · <a href="https://fiveclaw.xyz/dashboard/download">Get the setup guide</a>
</p>

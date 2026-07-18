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

`fiveclaw-agent` is a local [MCP](https://modelcontextprotocol.io) server that runs on your machine alongside your FiveM server. It gives your AI client (Claude Code, Cursor, Windsurf, etc.) direct access to your server's files, logs, MySQL database, SSH, and txAdmin — plus a full **local** toolkit for FiveM analysis: security scanning, resource validation, anti-pattern and duplicate detection, event tracing, dependency graphs, and more.

**Everything runs on your machine. No API key required to get started.** An optional free FiveClaw account adds the cloud docs library — 6,400+ FiveM/GTA natives and framework references (ESX, QBCore, ox).

**Install once. Works everywhere you code.**

---

## Quick Install

```bash
pip install fiveclaw-agent
```

That's it — every analysis and server tool runs locally with no account. Want native/framework docs too? Grab a free [API key](https://fiveclaw.xyz/dashboard/keys) and add it to your config.

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
  │      fiveclaw-agent         │  ← runs locally · no key needed
  │                             │
  │  ├─ 🛡  Security & analysis  │  scan · validate · trace · deps
  │  ├─ 📁 Resource map + search│
  │  ├─ 🗃  MySQL queries        │
  │  ├─ 🖥  Server control       │
  │  ├─ 📋 Log reader           │
  │  ├─ 🔑 SSH + deploy         │
  │  └─ 🧠 Persistent memory    │
  └──────────┬──────────────────┘
             │  HTTPS · free API key (docs only)
             ▼
  ┌─────────────────────────────┐
  │   FiveClaw Docs             │  6,400+ natives · ESX · QBCore · ox
  │   (free with an account)    │  framework refs · best practices
  └─────────────────────────────┘
```

Your credentials and code never leave your machine — the analysis runs locally. Only native/framework doc lookups reach FiveClaw, and those never include your code.

---

## Tools

Everything below runs **locally on your machine, no API key needed**, except the FiveClaw Docs section at the end.

### Analysis (local)

| Tool | What it does |
|------|-------------|
| 🛡 `scan_security` / `scan_security_all` | Detect injection, auth bypass, and logic vulnerabilities |
| 🏥 `resource_health_check` | Validate manifest, syntax, NUI build, dependencies |
| ✅ `tool_validate_resource` | Full resource validation (fxmanifest, Lua syntax, structure) |
| 🎯 `trace_event_flow` | Trace any event from trigger to handler across resources |
| 🗺 `show_dependency_graph` | Map every resource dependency across your server |
| 🔎 `detect_anti_patterns` / `detect_duplicate_code` | Flag common FiveM mistakes and copy-pasted logic |
| 📐 `validate_load_order` / `validate_export_contracts` | Catch load-order and export/import mismatches |
| 🔗 `find_exports` / `find_event_handlers` / `find_triggers` | Locate exports, handlers, and triggers anywhere |
| 🧩 `pattern_list` / `pattern_apply` | Scaffold resources from reusable templates |
| ✅ `tool_syntax_check` | Check Lua syntax (built-in checker — no external binary needed) |

### Server & files (local)

| Tool | What it does |
|------|-------------|
| 📂 `repomap_generate` / `tool_search` | Map and search all resources in your server |
| 🗃 `tool_mysql_query` / `mysql_list_databases` / `mysql_visualize_schema` | Query and inspect your FiveM MySQL databases |
| 🖥 `tool_server_control` / `tool_resource_control` | Start, stop, restart the FXServer or individual resources |
| 📡 `tool_server_console` | Send console commands |
| 📋 `read_latest_logs` | Tail FXServer and resource logs |
| 🔑 `tool_ssh_run/ls/read/write` · 🚀 `deploy_resource` | Full SSH access and deploy to your remote server |
| 🧠 `context_remember` / `context_recall` | Persistent notes across AI sessions |
| ℹ️ `tool_platform_info` | Show configured OS, paths, and enabled services |

### FiveClaw Docs (cloud — free with an account)

Native and framework reference, accessed with a free [API key](https://fiveclaw.xyz/dashboard/keys). Your code is never sent — these are read-only doc lookups.

| | |
|---|---|
| 📖 `fivem_native` | Full reference for all 6,400+ FiveM/GTA natives with examples |
| 🏗 `fivem_get_framework_docs` | ESX, QBCore, ox_lib, ox_core — guides, functions, patterns |
| 💡 `fivem_get_best_practice` | Lua performance, sync patterns, common pitfalls |
| ⚠️ `fivem_get_error_solution` | Common FiveM errors with step-by-step fixes |
| 🌐 `fivem_fetch_live_natives` | Fetch live documentation directly from CFX |

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

MySQL and Lua syntax checking work out of the box on every OS — the agent ships a built-in MySQL client and Lua checker, so there's no MariaDB/luac install or path setup.

**Windows note:** Use `python -u -m fiveclaw_agent` as the command (not the `fiveclaw` entry point) to avoid pipe-buffering issues. The setup wizard handles this automatically.

---

## Requirements

- Python 3.10+
- A free [FiveClaw account](https://fiveclaw.xyz) — optional, only for the cloud docs

---

## License

MIT — free to use, fork, and modify. Every tool in this package is open source and runs locally. The FiveClaw Docs service (native/framework reference) is a separate hosted service accessed with a free account key.

---

<p align="center">
  Built by <a href="https://fiveclaw.xyz">FiveClaw</a> · <a href="https://fiveclaw.xyz/dashboard/download">Get the setup guide</a>
</p>

<div align="center">

<img src="custom_components/claude_ha/brand/logo.png" alt="Claude for Home Assistant" width="380">

# Claude for Home Assistant

**Chat with Claude from Home Assistant Assist, and call it from your automations** —
powered by the companion Claude Code add-on running on your own hardware.

[![CI](https://github.com/LayerTM/claude-ha/actions/workflows/ci.yaml/badge.svg)](https://github.com/LayerTM/claude-ha/actions/workflows/ci.yaml)
[![Release](https://img.shields.io/github/v/release/LayerTM/claude-ha?sort=semver&color=41BDF5)](https://github.com/LayerTM/claude-ha/releases)
[![License](https://img.shields.io/github/license/LayerTM/claude-ha?color=blue)](LICENSE)
[![Quality scale](https://img.shields.io/badge/quality%20scale-platinum-E5E4E2)](https://developers.home-assistant.io/docs/core/integration-quality-scale/)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5?logo=homeassistant&logoColor=white)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.7%2B-41BDF5?logo=homeassistant&logoColor=white)](https://www.home-assistant.io)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)

</div>

---

`claude_ha` exposes Claude to Home Assistant itself. It talks to Claude through
the [Claude Code add-on](https://github.com/LayerTM/ClaudeInHA) over Home
Assistant's internal network — the add-on holds the login and runs Claude, while
this integration is a thin, secure client. **Nothing here calls the Anthropic
cloud directly.**

| | |
|---|---|
| 💬 **Conversation agent** | Talk to Claude from HA Assist (text or voice, any device), selectable like any other assistant. |
| ⚙️ **`claude_ha.ask` action** | Send a prompt to Claude from automations and scripts and use its answer. |
| 📟 **Status sensor** | Add-on readiness plus the running add-on/Claude versions and active model. |
| 🔒 **Secure by design** | Read-only by default, bearer-token auth, scoped writes, no cloud calls. |
| 🚀 **Zero-touch setup** | Discovered, installed and started for you via the Supervisor. |

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Security model](#security-model)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Removal](#removal)
- [Development](#development)

## How it works

```text
HA Assist ─┐
           ├─► claude_ha (this integration) ──HTTP + bearer──► Claude Code add-on ──► Claude
Automation ┘        conversation entity                        (own agentic loop,
                    claude_ha.ask action                         scoped HA access)
```

The integration and the add-on are separate projects that connect through a
small, versioned HTTP contract (a bearer-authenticated prompt server on the
add-on's internal port). The connection details — host, port and a shared token —
are handed to the integration automatically through Supervisor discovery, so
there is nothing to type in.

## Requirements

- Home Assistant OS or Supervised (the integration manages a Supervisor add-on).
- The [Claude Code add-on](https://github.com/LayerTM/ClaudeInHA) available in
  your add-on store (add its repository first).

## Installation

### HACS

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=LayerTM&repository=claude-ha&category=integration)

1. In HACS, add this repository as a **custom repository** (category *Integration*).
2. Install **Claude**, then restart Home Assistant.
3. Install the Claude Code add-on. Once it starts, Home Assistant offers to set
   up the **Claude** integration automatically — accept it. No configuration
   needed.

### Manual

Copy `custom_components/claude_ha` into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

Setup is zero-touch: when the Claude Code add-on starts it advertises its host,
port and a freshly generated token through Supervisor discovery, and Home
Assistant surfaces a one-click setup. You can also add it from **Settings →
Devices & services → Add integration → Claude**; it will find, install and start
the add-on for you. There are no options to fill in.

## Usage

### Conversation agent

Select **Claude** as a conversation agent under **Settings → Voice assistants**,
or target it directly. It answers in any language.

Every chat turn runs the add-on in **read-only** mode: Claude answers questions
and reads exposed Home Assistant state, but changes nothing. If a message would
change state, Claude returns a described *proposal* and the integration surfaces
it in the reply rather than acting on it.

### The `claude_ha.ask` action

```yaml
action: claude_ha.ask
data:
  prompt: Summarise today's calendar and suggest what to wear.
response_variable: claude
```

`claude.text` holds Claude's answer; the response also includes `proposal` (a
described state change, or `null`), `tools_used`, and `truncated`.

To act on something, use the two-phase flow: a read call returns a `proposal`
with `intents`; after your own confirmation, echo those exact `intents` back in a
`write` call. Writes are scoped on the add-on side to just those confirmed
intents — only use `write` from automations you control, never on untrusted input.

```yaml
# 1. Ask (read) — get the proposed intents.
- action: claude_ha.ask
  data:
    prompt: Turn off everything in the garage.
  response_variable: claude
# 2. Confirm, then act (write) — echo the confirmed intents back.
- action: claude_ha.ask
  data:
    prompt: Turn off everything in the garage.
    mode: write
    intents: "{{ claude.proposal.intents }}"
```

### Status sensor

`sensor.claude_code_status` reports `ready` / `initializing` and carries the
add-on version, Claude version and active model as attributes. It becomes
unavailable when the add-on is unreachable.

## Security model

The security-critical work lives in the add-on; this integration deliberately
does the least it can.

- **Bearer token, not network trust.** Every request carries a token issued via
  Supervisor discovery. It never appears in the UI and is redacted from
  diagnostics.
- **Read-only by default.** Chat is treated as untrusted input and runs
  deny-by-default; state changes require explicit, scoped, confirmed intents.
- **No cloud calls from HA.** The integration only talks to the local add-on.

See [SECURITY.md](SECURITY.md) for the reporting policy, and the add-on for the
full picture (env-scrubbed child processes, per-call statelessness, rate
limiting, output redaction and audit logging).

## How data is updated

The status sensor is refreshed by a `DataUpdateCoordinator` that polls the
add-on's `/api/status` endpoint every 60 seconds. Prompts (chat turns and the
`ask` action) are sent on demand.

## Known limitations

- Requires Home Assistant OS / Supervised — the add-on is a Supervisor add-on and
  is not available on Home Assistant Container or Core installs.
- One Claude instance per Home Assistant (one add-on → one config entry).
- The conversation agent surfaces proposed state changes but does not run a full
  confirm-and-act handshake from chat; use `claude_ha.ask` with `mode: write`
  from a trusted automation for that.

## Troubleshooting

- **"The Claude Code add-on is not running".** A repair issue offers to start it;
  or start it from the add-on page. Home Assistant retries setup automatically.
- **Setup keeps retrying.** Check that the add-on is installed, started and
  healthy; the status endpoint must be reachable on the internal network.
- **Diagnostics.** Download diagnostics from the integration's device page (the
  token is redacted) to inspect the last known status.

## Removal

Delete the **Claude** integration from **Settings → Devices & services**. If Home
Assistant installed the Claude Code add-on for you, remove it separately from the
add-on store.

## Development

```bash
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
pre-commit install

ruff check custom_components tests scripts
ruff format --check custom_components tests scripts
mypy custom_components/claude_ha
pytest --cov=custom_components.claude_ha --cov-report=term-missing
python scripts/secret_scan.py .
```

CI runs hassfest, HACS validation, ruff, mypy, the test suite (100% coverage) and
a secret scan on every push and pull request. See [CONTRIBUTING.md](CONTRIBUTING.md).

Brand images live under `custom_components/claude_ha/brand/` and are served by
Home Assistant's Brands Proxy API (2026.3+) — no separate brands submission
needed.

## License

[MIT](LICENSE) © LayerTM

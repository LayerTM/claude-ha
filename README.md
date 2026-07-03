<div align="center">

<img src="custom_components/claude_ha/brand/logo.png" alt="Claude for Home Assistant" width="360">

# Claude for Home Assistant

Chat with **Claude** from Home Assistant Assist, and call it from your
automations — powered by the companion **Claude Code** add-on running on your
own hardware.

[![HACS: custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![CI](https://github.com/LayerTM/claude-ha/actions/workflows/ci.yaml/badge.svg)](https://github.com/LayerTM/claude-ha/actions/workflows/ci.yaml)
[![Quality scale](https://img.shields.io/badge/quality%20scale-platinum-E5E4E2.svg)](https://developers.home-assistant.io/docs/core/integration-quality-scale/)

</div>

## What it is

`claude_ha` is a Home Assistant integration that exposes Claude to Home
Assistant itself:

- **A conversation agent** — talk to Claude from HA Assist (text or voice, on any
  device), selectable like any other assistant.
- **A `claude_ha.ask` action** — send a prompt to Claude from automations and
  scripts and use its answer.
- **A status sensor** — shows whether the add-on is ready, plus the running
  add-on and Claude versions and active model.

It talks to Claude through the [Claude Code add-on](https://github.com/LayerTM/ClaudeInHA)
over Home Assistant's internal network. The add-on holds the login/credentials
and runs Claude; this integration is a thin, secure client. Nothing here calls
the Anthropic cloud directly.

## How it works

```
HA Assist ─┐
           ├─► claude_ha (this integration) ──HTTP+bearer──► Claude Code add-on ──► Claude
Automation ┘        conversation entity                       (own agentic loop,
                    claude_ha.ask action                        scoped HA access)
```

The integration and the add-on are two separate projects that connect through a
small, versioned HTTP contract (a bearer-authenticated prompt server on the
add-on's internal port). The connection details (host, port and a shared token)
are handed to the integration automatically through Supervisor discovery — there
is nothing to type in.

## Requirements

- Home Assistant OS / Supervised (the integration manages a Supervisor add-on).
- The [Claude Code add-on](https://github.com/LayerTM/ClaudeInHA) available in
  your add-on store (add its repository first).

## Installation

### HACS (recommended)

1. In HACS, add this repository as a **custom repository** (category:
   *Integration*).
2. Install **Claude**, then restart Home Assistant.
3. Install the Claude Code add-on (add its repository, then install it). Once it
   starts, Home Assistant will offer to set up the **Claude** integration
   automatically. Accept it — no configuration is needed.

### Manual

Copy `custom_components/claude_ha` into your Home Assistant `config/custom_components/`
directory and restart.

## Configuration

Setup is zero-touch: when the Claude Code add-on starts it advertises its host,
port and a freshly generated token through Supervisor discovery, and Home
Assistant surfaces a one-click setup. If you prefer, add the integration from
**Settings → Devices & services → Add integration → Claude**; it will find,
install and start the add-on for you. There are no options to fill in.

## Usage

### Conversation agent

Select **Claude** as a conversation agent under **Settings → Voice assistants**,
or target it directly. It answers in any language.

For safety, every chat turn runs the add-on in **read-only** mode: Claude can
answer questions and read exposed Home Assistant state, but it does not change
anything. If Claude determines a message would change state, it returns a
described *proposal* instead of acting, and the integration surfaces that
proposal in the reply rather than executing it.

### The `claude_ha.ask` action

```yaml
action: claude_ha.ask
data:
  prompt: Summarise today's calendar and suggest what to wear.
response_variable: claude
```

`claude.text` holds Claude's answer. The response also includes `proposal` (a
described state change, or `null`), `tools_used`, and `truncated`.

To act on something, use the two-phase flow: a read call returns a described
`proposal` (with `intents`); after your own confirmation, echo those exact
`intents` back in a `write` call. Write mode is scoped on the add-on side to
just those confirmed intents — only use it from automations you control, never
on untrusted input.

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

## How data is updated

The status sensor is refreshed by a `DataUpdateCoordinator` that polls the
add-on's `/api/status` endpoint every 60 seconds. Prompts (chat turns and the
`ask` action) are sent on demand.

## Security model

The security-critical work lives in the add-on; this integration deliberately
does the *least* it can. Highlights of the contract it relies on:

- **Bearer token, not IP trust.** Every request carries a shared token issued via
  Supervisor discovery. The token never appears in the UI and is redacted from
  diagnostics.
- **Read-only by default.** Chat is treated as untrusted input and runs
  deny-by-default with a read-only tool scope; state changes require an explicit,
  scoped write request.
- **No cloud calls from HA.** The integration only talks to the local add-on.

See the add-on for the full picture (env-scrubbed child processes, per-call
statelessness, rate limiting, output redaction and audit logging).

## Known limitations

- Requires Home Assistant OS / Supervised — the add-on is a Supervisor add-on and
  is not available on Home Assistant Container or Core installs.
- One Claude instance per Home Assistant (one add-on → one config entry).
- The conversation agent surfaces proposed state changes but does not execute a
  full confirm-and-act handshake from chat; use the `ask` action with `mode:
  write` from a trusted automation for that.

## Troubleshooting

- **"The Claude Code add-on is not running".** A repair issue offers to start it;
  or start it from the add-on page. Home Assistant retries setup automatically.
- **Setup keeps retrying.** Check that the add-on is installed, started and
  healthy; the status endpoint must be reachable on the internal network.
- **Diagnostics.** Download diagnostics from the integration's device page (the
  token is redacted) to inspect the last known status.

## Removal

Delete the **Claude** integration entry from **Settings → Devices & services**.
If Home Assistant installed the Claude Code add-on for you, remove it separately
from the add-on store.

## Development

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
ruff check custom_components tests
ruff format --check custom_components tests
mypy custom_components/claude_ha
pytest --cov=custom_components.claude_ha --cov-report=term-missing
```

CI runs hassfest, HACS validation, ruff, mypy, the test suite and a secret scan
on every push and pull request.

## Brand assets

Brand images live under `custom_components/claude_ha/brand/` and are served
directly by Home Assistant's Brands Proxy API (2026.3+) — no submission to a
separate brands repository is required.

## License

[MIT](LICENSE) © LayerTM

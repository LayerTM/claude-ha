# Security Policy

This integration is the **client** half of a deliberately security-hardened
bridge to the Claude Code add-on, which runs a large language model on
potentially untrusted input. Security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately via GitHub Security Advisories:
[**Report a vulnerability**](https://github.com/LayerTM/claude-ha/security/advisories/new).

Include the affected version, Home Assistant version, a description of the
issue, and a proof of concept if you have one. You can expect an initial
response within a few days.

## Supported versions

Security fixes are provided for the latest released version. Because the
integration tracks current Home Assistant releases, please reproduce on a
supported Home Assistant version before reporting.

## Security model (what the integration guarantees)

The heavy lifting lives in the add-on; the integration is intentionally minimal.
Its guarantees:

- **No cloud calls.** The integration only talks to the local add-on over Home
  Assistant's internal network. It never contacts the Anthropic API directly.
- **Bearer token, not network trust.** Every request carries a shared token
  provisioned via Supervisor discovery. The token is never shown in the UI and
  is redacted from diagnostics.
- **Read-only by default.** Conversation turns are treated as untrusted input
  and always run the add-on in read-only mode. Claude cannot change Home
  Assistant state from chat; it returns a described *proposal* instead.
- **Scoped writes only.** The `claude_ha.ask` action's `write` mode requires the
  explicit, user-confirmed `intents` echoed from a prior read-mode proposal (at
  most five). Free-text writes are rejected before the add-on is contacted.
- **Secret-scanned.** Every change is scanned (`scripts/secret_scan.py`, in
  pre-commit and CI) for tokens, keys and personal data.

If you are integrating or auditing, the request/response contract and its
security rationale are documented alongside the add-on.

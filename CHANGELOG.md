# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.4] - 2026-07-03

### Fixed

- The discovered-add-on confirm step no longer renders a `{addon}` placeholder
  error — the add-on name is now passed as a `description_placeholders` value
  (previously only the flow title had it, so the step description was unfilled).

## [0.1.3] - 2026-07-03

### Added

- **Usage sensors** (needs the Claude Code add-on ≥ 1.7.0): *Token usage today*
  (input + output tokens, with the full `/api/usage` report as attributes) and
  *Prompt API cost* (total USD). Backed by a separate slow (~5 min) coordinator.
- `ha_mcp` attribute on the status sensor (from `/api/status`).

## [0.1.2] - 2026-07-03

### Added

- Bundled Lovelace **chat card** (`custom:claude-chat-card`), served by the
  integration itself — chat with Claude and Apply/Dismiss its proposals inline.
- Optional `notify` field on `claude_ha.ask`: when Claude proposes a change, send
  an actionable **Approve / Dismiss** mobile notification; Approve runs the
  confirmed write (with the proposal's intents) and logs it to the logbook.

## [0.1.1] - 2026-07-03

### Changed

- Split CI into per-workflow files (hassfest, hacs, lint, tests, secret-scan),
  each with least-privilege permissions.
- Reworked the README badge set and brand assets; added community health files
  (security policy, contributing guide, code of conduct, issue/PR templates,
  Dependabot).

## [0.1.0] - 2026-07-03

Initial release.

### Added

- Conversation agent (`conversation.claude_code`) that forwards HA Assist
  messages to Claude via the Claude Code add-on, running read-only and surfacing
  proposed state changes without executing them.
- `claude_ha.ask` action returning Claude's answer plus any proposal, the tools
  it used, and whether output was truncated; opt-in `write` mode that requires
  the user-confirmed intents echoed from a prior read-mode proposal.
- Status sensor exposing add-on readiness, add-on/Claude versions and the active
  model.
- Zero-touch setup via Supervisor discovery, with config-flow install/start of
  the add-on and a repair issue + fix flow when it is stopped.
- Diagnostics (bearer token redacted) and locally bundled brand assets.
- Full test suite (100% coverage), strict typing, and CI running hassfest, HACS
  validation, ruff, mypy, pytest and a secret scan.

[Unreleased]: https://github.com/LayerTM/claude-ha/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/LayerTM/claude-ha/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/LayerTM/claude-ha/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/LayerTM/claude-ha/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/LayerTM/claude-ha/releases/tag/v0.1.0

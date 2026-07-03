# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Conversation agent (`conversation.claude_code`) that forwards HA Assist
  messages to Claude via the Claude Code add-on, running read-only and surfacing
  proposed state changes without executing them.
- `claude_ha.ask` action returning Claude's answer plus any proposal, the tools
  it used, and whether output was truncated; supports an opt-in `write` mode for
  trusted automations.
- Status sensor exposing add-on readiness, add-on/Claude versions and the active
  model.
- Zero-touch setup via Supervisor discovery, with config-flow install/start of
  the add-on and a repair issue + fix flow when it is stopped.
- Diagnostics (bearer token redacted) and bundled brand assets.
- Full test suite (100% coverage), and CI running hassfest, HACS validation,
  ruff, mypy, pytest and a secret scan.

[Unreleased]: https://github.com/LayerTM/claude-ha/commits/main

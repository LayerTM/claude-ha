# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.0] - 2026-07-09

### Added

- **Create automations by describing them, with a confirmation.** Ask Claude to
  create an automation and it drafts the config, shows it to you, and — when you
  reply "yes" — creates it in Home Assistant (voice gets a short spoken "…say yes
  to confirm"). Before anything is written, the draft is re-validated against Home
  Assistant's own automation schema and passed through a strict safety allow-list:
  it may only call everyday device, helper and notify services, and any draft that
  reaches for a shell command, a script runtime, a templated service name, a way to
  reproduce arbitrary entity states across other domains, or any service outside
  that set is refused — not created. Nothing is written unless the whole draft
  passes. Needs the Claude add-on ≥ 1.34.0.

## [1.1.1] - 2026-07-09

### Fixed

- **CI type-check (mypy strict) on 1.1.0.** The automation-draft rendering added in
  1.1.0 tripped strict typing: PyYAML had no type stubs, and the drafted-config
  argument wasn't narrowed from `dict | None` at the call boundary. Added the
  `types-PyYAML` stub to the test requirements and narrowed the value before use.
  No behavior change; internal only.

## [1.1.0] - 2026-07-09

### Added

- **Describe an automation in words and see it drafted (preview).** Ask Claude to
  create an automation ("create an automation that turns on the porch light at
  sunset") and it now drafts the full Home Assistant config and shows it to you as
  a YAML block you can read or copy — read-only, nothing is written yet. On voice
  the reply stays a short spoken summary (the YAML isn't read aloud). Needs the
  Claude add-on ≥ 1.34.0; with an older add-on nothing changes. Committing a draft
  with a confirmation is coming in a later release.

## [1.0.1] - 2026-07-09

### Fixed

- **Usage dashboard: the cost history was unreadable.** The example plotted API
  cost (USD, ~0–5) and token usage (thousands–millions) on one history graph;
  because a graph shares a single y-axis, the cost line was flattened to the
  bottom and effectively invisible. Cost and tokens are now split into two graphs,
  each auto-scaling to its own range. Also documented that the budget gauge is only
  meaningful when a daily budget is set on the add-on. Docs-only; re-copy the
  dashboard YAML from the README if you added it.

## [1.0.0] - 2026-07-09

First stable release. The integration has been in daily live use and is
feature-complete across chat, voice, camera vision, health checks and usage
reporting, at Home Assistant's **platinum** quality scale with 100% test
coverage. 1.0.0 marks the public surface below as stable — future changes keep it
backward-compatible or land in a new major.

### Stable surface

- **Conversation agent** — Claude as an Assist conversation entity that can read
  and (with confirmation) act on Home Assistant; benign low-risk actions run
  immediately, everything else is held for a plain yes/no confirmation.
- **Voice** — concise, spoken-friendly replies on voice turns (one short sentence,
  no markdown), with a one-click local Whisper + Piper Assist pipeline setup.
- **Camera vision** — Claude can look at an Assist-exposed camera a question
  clearly refers to.
- **Sensors** — Status, Chat health, Daily budget spend, Token usage today and
  Prompt API cost, plus a ready-made built-in-card **usage dashboard** (see the
  README).
- **Health checks & repairs** — proactive detection of the "chat can't see your
  home" failures, each with an actionable repair.
- **The `claude_ha.ask` action** and the shipped Claude chat card.

### Added

- **Usage dashboard** — a copy-paste Lovelace overview (cost, budget gauge, chat
  health, MCP state, token/cost history) built entirely from Home Assistant's
  built-in cards; no custom resource to install. Documented in the README.

### Documentation

- The Sensors section now lists all five sensors with their entity ids and the
  add-on version each needs.

## [0.1.17] - 2026-07-09

### Changed

- **The "Model Context Protocol Server unreachable" repair no longer flashes on a
  brief transient.** The add-on's connectivity signal can momentarily read
  "disconnected" for a single status poll (e.g. on a chat turn right after a
  restart) even though it is working. The integration now waits for two
  consecutive polls to agree before raising that repair, so a one-off blip no
  longer pops a scary error that then clears itself. A genuinely missing Model
  Context Protocol Server integration is still reported immediately — only the
  transient blip is debounced. (Defense-in-depth on top of the add-on's own
  root-cause fix in 1.30.0; no add-on update is required for this change.)

## [0.1.16] - 2026-07-09

### Added

- **Voice replies are now concise.** When a turn arrives by voice (a voice
  satellite, the companion-app microphone, or VoIP — anything Home Assistant
  will speak back through text-to-speech), the integration now tells the add-on
  the turn is spoken, so Claude answers in one short, natural sentence instead of
  a paragraph with markdown, lists or URLs that sound like noise aloud. Typed
  chat is unchanged. Voice confirmations were shortened to match — instead of a
  multi-line proposal ending in "Confirm? (yes/no)", a spoken turn now asks a
  brief "…Say yes to confirm." This needs the Claude add-on at version 1.28.0 or
  newer; with an older add-on the integration stays on its previous behaviour.

## [0.1.15] - 2026-07-05

### Fixed

- **No more spurious "Camera vision is on, but no cameras are exposed" repair right
  after a restart.** Like the earlier Model-Context-Protocol-Server case, the
  camera-exposure guard was evaluating before Home Assistant finished loading the
  camera entities, so it briefly saw zero exposed cameras and raised a repair that
  then cleared itself. It now waits until Home Assistant has fully started before
  concluding no cameras are exposed.

### Fixed

- **Camera vision now matches a camera's location even when the spelling differs
  by a space, hyphen or case.** A camera in an area named "Frontyard" was not
  matched when you said "front yard" (and, with two channels exposed, vision
  silently answered from state instead of looking). Matching is now
  separator-insensitive on both sides ("Frontyard" = "front yard" = "front-yard"),
  and a location embedded in parentheses in a long name (e.g. "G4 Instant (Front
  Yard) High Resolution Channel") is matched too. The exposed-only ceiling and the
  never-guess-between-cameras rule are unchanged. Resolving now also logs at debug
  level to make future diagnosis easy.

### Added

- **A "Daily budget spend" diagnostic sensor.** With a recent add-on (≥ 1.21.0)
  that reports a daily spend cap, `sensor.*_budget` shows today's spend, with the
  cap, remaining, fraction used and a soft "near the cap" flag as attributes — a
  glanceable indicator, never a repair. An unlimited cap leaves the cap-derived
  attributes empty; older add-ons leave the sensor unavailable.

### Changed

- **The request timeout now follows the add-on's own budget.** Instead of a fixed
  135 s, the client reads the add-on's reported prompt timeout (add-on ≥ 1.21.0)
  and keeps its own wall-clock a margin above it, so if you raise the add-on's
  timeout, a slow answer still arrives instead of being cut off client-side. It
  never drops below the 135 s floor, and falls back to it on older add-ons.

## [0.1.12] - 2026-07-05

### Fixed

- **Multi-channel cameras no longer confuse camera vision.** A UniFi-style camera
  exposes several channels (high/medium) of the same physical camera. If you
  exposed more than one, a visual question matched them all and the "never guess
  between cameras" rule declined — so nothing was sent. Channels of the *same*
  camera are now collapsed to one (the high-resolution one), so it resolves;
  genuinely different cameras still stay ambiguous.
- **Cameras named "… Resolution Channel" now match by location.** A camera whose
  name carries a channel suffix (e.g. "Front Yard High Resolution Channel") is now
  matched when you just say its location ("Front yard"), by also matching the name
  with that channel suffix stripped.

### Added

- **A "Chat health" diagnostic sensor.** When paired with a recent add-on
  (≥ 1.20.0), a new `sensor.*_chat_health` shows `ok` / `degraded` at a glance and
  carries the rolling counts as attributes — `recent`, `degraded` (reads that
  failed even after a retry), `recovered` (reads a retry quietly rescued), and
  `last_reason` (a short failure-reason token, never your prompt text). It's a soft
  indicator, deliberately **not** a repair, so occasional transient blips don't
  nag you. On older add-ons that don't report it, the sensor stays unavailable.

## [0.1.10] - 2026-07-05

### Added

- **A repair now tells you when camera vision is on but no camera is visible to
  Claude.** Home Assistant hides cameras from Assist by default (they count as
  security devices), so turning on "Let Claude look at cameras" did nothing until
  you also exposed a camera — with no hint why. The integration now raises a
  guided repair when vision is enabled but zero cameras are exposed to Assist,
  pointing you to the expose page. It clears itself once you expose one.

### Fixed

- **No more spurious "Claude can't reach the Model Context Protocol Server" repair
  right after a restart.** The health check treated the `mcp_server` integration
  as missing while Home Assistant was still starting it up, flashing a repair that
  cleared itself moments later. It now waits until Home Assistant has fully started
  before concluding the server is genuinely missing.

### Added

- **The conversation language is now sent to the add-on** (additive `language`
  field on `/api/prompt`, from `user_input.language`) so the add-on can localize
  its own server-authored messages — the "couldn't finish that response" apology
  and the daily-budget notice — to the user's language instead of always English.
  Backward-compatible: an add-on that doesn't use it simply ignores it, and a
  missing language falls back to English (needs the matching add-on release to see
  a localized message).

## [0.1.8] - 2026-07-05

### Fixed

- **Camera vision now recognises the camera by its location.** A visual question
  that named a camera by its **device name** or a registered **Assist alias**
  (e.g. "look at the Front yard camera") resolved to nothing, so no snapshot was
  ever sent — camera name matching only considered the entity's friendly name,
  area and floor. It now also matches the device name and the user's explicit
  aliases (English and Ukrainian). The Assist-exposed-only ceiling and the
  never-guess-between-cameras rule are unchanged. Confirmed against live Home
  Assistant; no add-on update required (works with add-on ≥ 1.17.0).
- **Request timeout raised 120 s → 135 s** so a read that the add-on (≥ 1.18.0)
  gracefully recovers or degrades right at its own 120 s budget delivers its
  friendly answer instead of being cut off by a client-side timeout. Normal reads
  finish well under this; the change only affects the rare at-the-ceiling case.

## [0.1.7] - 2026-07-04

### Added

- **Streaming replies**: the conversation agent now streams Claude's answer token
  by token instead of waiting for the whole response (add-on ≥ 1.17.0, NDJSON).
  Older add-ons that return a single JSON body still work — the client detects
  the response type. The hybrid auto/confirm behaviour is unchanged.
- **Camera vision** (opt-in, off by default; add-on ≥ 1.17.0): a new *Let Claude
  look at cameras* option. A clearly visual question sends one snapshot of an
  Assist-exposed camera to Claude, resolved by name/area/floor; ambiguous cases
  never guess. The integration passes only the camera entity id.

### Security

- A camera snapshot is only ever sent for a camera exposed to Assist
  (`async_should_expose`), only when vision is enabled and exactly one camera
  resolves. Streaming remains read-only; writes are never streamed.

## [0.1.6] - 2026-07-04

### Added

- **Health checks**: the integration now detects the "chat can't see your home"
  gaps — Claude not logged in, no HA token, the Model Context Protocol Server
  integration missing/unreachable, or nothing exposed to Assist — and raises a
  repair with the exact fix. Evaluated on each status poll (no Claude cost); the
  new **Check Claude health** button runs a deeper reachability probe. The status
  sensor gains `health`, `ha_mcp_connected` and `exposed_to_assist` attributes.
  Reachability needs the Claude Code add-on ≥ 1.14.0.
- **Local voice one-click**: a `claude_ha.setup_voice` action that installs and
  starts the Whisper (STT) and Piper (TTS) add-ons for a chosen language and
  creates an Assist pipeline wired to the Claude conversation agent.

## [0.1.5] - 2026-07-04

### Added

- **The conversation agent now acts on Home Assistant** (needs the Claude Code
  add-on ≥ 1.8.0): benign, low-risk actions run immediately; important ones are
  held and confirmed with a plain yes/no. Criticality is judged per action from
  live entity metadata (`risk.py`) — the model's risk hint is only advisory.
- Options flow: *auto-execute* toggle and an *always-confirm entities* list.
- `confirmation` ("auto"/"confirmed") on the client's write call.

### Security

- Auto-execution requires BOTH the model's `risk == "low"` AND the deterministic
  classifier clearing every target; confirmed writes replay only the stored,
  validated intents, never the new chat text.
- The classifier fails closed: it auto-runs only actions it can positively prove
  benign. A cover's device class is resolved from live state as well as the
  registry (registry-less garage doors are still caught); opaque-effect wrappers
  (scene/script/automation) always confirm; intents with empty targets or
  entity-routing `data` slots are never auto-executed; and malformed
  model-supplied intents are treated as critical instead of raising.

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

[Unreleased]: https://github.com/LayerTM/claude-ha/compare/v0.1.15...HEAD
[0.1.15]: https://github.com/LayerTM/claude-ha/compare/v0.1.14...v0.1.15
[0.1.14]: https://github.com/LayerTM/claude-ha/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/LayerTM/claude-ha/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/LayerTM/claude-ha/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/LayerTM/claude-ha/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/LayerTM/claude-ha/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/LayerTM/claude-ha/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/LayerTM/claude-ha/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/LayerTM/claude-ha/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/LayerTM/claude-ha/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/LayerTM/claude-ha/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/LayerTM/claude-ha/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/LayerTM/claude-ha/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/LayerTM/claude-ha/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/LayerTM/claude-ha/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/LayerTM/claude-ha/releases/tag/v0.1.0

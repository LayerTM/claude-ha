"""Constants for the Claude for Home Assistant integration."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "claude_ha"
LOGGER: Final = logging.getLogger(__package__)

# The companion add-on's Supervisor slug is repository-prefixed and therefore
# varies per install (e.g. "abc123de_claude-code", "local_claude-code"), so it
# is resolved at runtime — from the discovery payload or by matching this
# suffix against installed/store add-ons — never hardcoded.
ADDON_SLUG_SUFFIX: Final = "_claude-code"
ADDON_NAME: Final = "Claude Code"

# Manufacturer/model shown on the HA device.
MANUFACTURER: Final = "Anthropic"

# Config-entry data keys.
CONF_ADDON_SLUG: Final = "addon_slug"
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_TOKEN: Final = "token"
CONF_USE_ADDON: Final = "use_addon"

# Options (additive; not part of the config-entry data schema).
CONF_CRITICAL_ENTITIES: Final = "critical_entities"
CONF_AUTO_EXECUTE: Final = "auto_execute"
# Camera vision (add-on >= 1.17.0): opt-in, off by default because a snapshot is
# more sensitive than state. Only ever an Assist-exposed camera is sent.
CONF_CAMERA_VISION: Final = "camera_vision"

# Default prompt-server port (contract §4; bound 0.0.0.0, not published to host).
DEFAULT_PORT: Final = 8126

# The add-on option that holds the shared bearer token in the discovery-less
# fallback path (contract §1).
ADDON_OPTION_API_TOKEN: Final = "api_token"

# --- Add-on HTTP contract (see .research/CONTRACT.md) -----------------------
API_PROMPT: Final = "/api/prompt"
API_STATUS: Final = "/api/status"
API_USAGE: Final = "/api/usage"

HEADER_CALLER: Final = "X-Claude-Caller"

# Request modes for POST /api/prompt.
MODE_READ: Final = "read"
MODE_WRITE: Final = "write"
MODES: Final = (MODE_READ, MODE_WRITE)

# Contract §2: `mode:"write"` MUST carry the user-confirmed proposal intents
# (echoed verbatim from a prior read-mode proposal), at most this many, and they
# are forbidden for `mode:"read"`.
MAX_WRITE_INTENTS: Final = 5

# Write confirmation levels (contract, add-on >= 1.8.0). "auto" is refused by the
# add-on's coarse domain backstop for inherently critical domains.
CONFIRMATION_AUTO: Final = "auto"
CONFIRMATION_CONFIRMED: Final = "confirmed"

# Per-intent risk hint carried on a proposal (untrusted model output; a hint,
# never the sole gate). Absent => treat as sensitive.
INTENT_RISK: Final = "risk"
RISK_LOW: Final = "low"
RISK_SENSITIVE: Final = "sensitive"

# Prompt-server hard cap on prompt size (bytes) — mirrored client-side so callers
# get a clean validation error instead of a 413.
PROMPT_MAX_BYTES: Final = 8192

# POST /api/prompt 200-response keys.
RESP_TEXT: Final = "text"
RESP_PROPOSAL: Final = "proposal"
RESP_TOOLS_USED: Final = "tools_used"
RESP_TRUNCATED: Final = "truncated"
PROPOSAL_SUMMARY: Final = "summary"
PROPOSAL_INTENTS: Final = "intents"
# Optional model-drafted automation config (add-on >= 1.34.0): a full HA automation
# dict (alias/triggers/conditions/actions/mode) on a read where the user asked to
# create one. OMITTED when there's no draft (absent-not-null), so key on presence.
# The integration only DISPLAYS the draft; committing it (after a confirmation) is a
# later capability that re-validates and writes it in-process — the add-on never
# writes it.
RESP_AUTOMATION: Final = "automation"

# Streaming read (add-on >= 1.17.0): opt-in via this request field; the add-on
# answers application/x-ndjson (one JSON object per line) or, when it can't
# stream, a normal JSON body — the client branches on the response Content-Type.
REQUEST_STREAM: Final = "stream"
CONTENT_TYPE_NDJSON: Final = "application/x-ndjson"
STREAM_KIND: Final = "type"
STREAM_KIND_DELTA: Final = "delta"
STREAM_KIND_DONE: Final = "done"
STREAM_KIND_ERROR: Final = "error"
STREAM_ERROR: Final = "error"

# Optional camera snapshot for a read (add-on >= 1.17.0): a camera entity_id the
# add-on fetches, downscales and lets Claude see. Only ever an Assist-exposed
# camera; the integration passes the entity_id, never a path.
REQUEST_IMAGE_ENTITY: Final = "image_entity"
# Optional HA conversation language (e.g. "uk"/"en"/"pl") so the add-on can localize
# its server-authored messages (the degraded-read apology, the budget notice). Purely
# additive: an add-on that doesn't read it — or a missing value — falls back to English.
REQUEST_LANGUAGE: Final = "language"
# Optional surface hint (add-on >= 1.28.0): "voice" when the turn will be spoken aloud
# by text-to-speech, else "text". On "voice" the add-on tells the model to keep the
# answer to one short, plain, speakable sentence. Unlike the older additive fields, a
# pre-1.28.0 add-on validates request keys against an allowlist and 400s on an unknown
# one, so the client only sends this when the add-on is new enough (see below).
REQUEST_SURFACE: Final = "surface"
SURFACE_VOICE: Final = "voice"
SURFACE_TEXT: Final = "text"
# First add-on version that accepts REQUEST_SURFACE (see above — the client
# send-gates the field on this version so older add-ons never see an unknown key).
ADDON_MIN_SURFACE_VERSION: Final = "1.28.0"
# Optional current config of the automation the user asked to modify (add-on >=
# 1.36.0). When present, the add-on tells the model to edit THIS config per the
# request and return the full updated automation in the `automation` response field.
# Send-gated on the version below, like REQUEST_SURFACE.
REQUEST_EDIT_AUTOMATION: Final = "edit_automation"
ADDON_MIN_EDIT_VERSION: Final = "1.36.0"

# GET /api/status 200-response keys.
STATUS_READY: Final = "ready"
STATUS_VERSION: Final = "version"
STATUS_CLAUDE_VERSION: Final = "claude_version"
STATUS_MODEL: Final = "model"
STATUS_HA_MCP: Final = "ha_mcp"
# Whether the LAST chat read actually reached the HA MCP server (add-on >= 1.14.0;
# null until the first read). Distinct from ha_mcp, which only says a config exists.
STATUS_HA_MCP_CONNECTED: Final = "ha_mcp_connected"
# Rolling chat-reliability summary (add-on >= 1.20.0): {recent, degraded, recovered,
# last_reason}. last_reason is a reason TOKEN from the runner's enum, never prompt
# content. Absent on older add-ons (the chat-health sensor is then unavailable).
# Add-on >= 1.49.0 stamps each entry and adds {last_failure_ts, window_from_ts,
# window_to_ts} in epoch MILLISECONDS, where null means UNKNOWN (history written by
# an older add-on is kept, unstamped) — never "now" and never 0. The same version
# adds consecutive_ok: successful runs recorded AFTER the last failure, counted by
# entry order rather than by clock, so it is a plain number even on unstamped
# history — never null, and simply absent on an older add-on. consecutive_failed is
# its mirror (failures since the last success); the two are never both non-zero,
# because the newest run is either a success or it isn't.
STATUS_CHAT_HEALTH: Final = "chat_health"
# The add-on's whole-request prompt budget in ms (add-on >= 1.21.0). The client keeps
# its wall-clock just above this so the add-on's graceful timeout answer always lands.
STATUS_PROMPT_TIMEOUT_MS: Final = "prompt_timeout_ms"
# Daily spend cap (add-on >= 1.21.0): {limit, spent} in USD; limit 0 means unlimited.
STATUS_BUDGET: Final = "budget"
# Active proactive-alert set (add-on >= 1.39.0): {active, critical, items} where each
# item is {key, critical, line}, or null when proactive_alerts has never run. Absent
# on older add-ons (the alerts binary sensor is then unavailable). items/line carry
# the user's OWN home entity names and readings — home data, not chat content.
STATUS_ALERTS: Final = "alerts"

# --- Chat-health thresholds -------------------------------------------------
# The add-on trims its chat-health window by COUNT (cap 50), never by age, and
# says so deliberately: it reports what happened and when, and leaves "what counts
# as healthy" to the consumer — these two constants are that judgement.
#
# Share of the window that must have failed (after a retry) before the sensor reads
# degraded — read as "one chat in ten or more is still failing"; the comparison is
# `>=`, so exactly one in ten raises it. The old rule —
# any failure at all — pinned the sensor to degraded until 50 further chats pushed
# the blip out, which on a quiet install is days.
#
# The rate is the NECESSARY condition; recovery and staleness below can only ever
# clear a warning, never raise one. That asymmetry is the design — frequency says a
# problem is real, recency only says it is current — but it puts the whole weight
# of raising the alarm on this one number, so it is calibrated for sensitivity
# rather than for patience: an old isolated failure is now caught by the two
# rescues, and no longer needs a lenient threshold to stay quiet. At 0.2 a steady
# one-in-six failure rate read "ok" forever, and a total outage needed ten
# consecutive failures to show up in a full 50-chat window; at 0.1 those become
# degraded and five.
#
CHAT_HEALTH_DEGRADED_RATE: Final = 0.1
# Failures in a row that mean an outage rather than a coincidence, whatever the
# rate says (add-on >= 1.49.0). This is the ONE signal that raises the state
# early: a rate over a count-trimmed window cannot see a fresh outage until it has
# diluted the window, which in a full 50-chat window is five failures deep.
#
# Three is a judgement, and what follows is the measurement it rests on rather
# than the intuition it used to. The only regime where this clause decides
# anything is BELOW the rate threshold — above it the rate has already fired and
# the run merely gets there sooner. So the false-alarm question is: how often does
# a merely-flaky install produce a trailing run of three by chance?
#
# Simulated, 200k windows of 50 at each rate:
#
#   fail rate   P(trailing run >= 2)   P(trailing run >= 3)
#   0.50                     24.93%                 12.49%   <- rate already fires
#   0.10                      1.01%                  0.11%   <- at the threshold
#   0.05                      0.24%                  0.01%   <- run is the only signal
#   0.02                      0.04%                  0.00%
#
# Two would also be defensible; it costs roughly 20x the false alarms in that
# regime, which is 0.24% against 0.01%. One is not: a single isolated failure is
# the complaint this whole rule exists to stop shouting about.
#
# The earlier justification here claimed an install failing every second chat
# "almost never" produces three in a row. That is false for a random process —
# 12.49% above — and true only of a DETERMINISTIC alternating pattern, whose
# trailing run is 1 at every period and phase. The conclusion survived; the reason
# given for it did not.
CHAT_HEALTH_OUTAGE_RUN: Final = 3
# How old the last recorded failure must be before it stops counting against health,
# whatever the rate. Needs `last_failure_ts` (add-on >= 1.49.0); an unstamped window
# falls back to the rate alone, so a MISSING stamp can never clear a warning by
# itself.
CHAT_HEALTH_STALE_FAILURE_S: Final = 6 * 60 * 60

# --- Timings ----------------------------------------------------------------
# How long the integration waits for a single Claude answer (a full agentic run
# in the add-on can take a while). Must stay ABOVE the add-on's whole-request
# budget (`CLAUDE_PROMPT_TIMEOUT_MS`, default 120s — retry runs on the remaining
# budget, not a fresh one, so the ceiling is 120s not 2x) so the add-on's graceful
# terminal `done` on a timed-out read always arrives before we abort; otherwise the
# user gets a hard timeout instead of the friendly degraded message (add-on >=1.18.0).
# If the add-on timeout is raised, raise this to match (budget + ~15s headroom).
# Floor value; the client tracks the add-on's reported `prompt_timeout_ms` and uses
# max(REQUEST_TIMEOUT, that + TIMEOUT_MARGIN) so its graceful answer always lands.
REQUEST_TIMEOUT: Final = 135
# Headroom (seconds) the client keeps above the add-on's prompt budget.
TIMEOUT_MARGIN: Final = 15
# Shorter timeout for the lightweight status poll.
STATUS_TIMEOUT: Final = 15
# Coordinator poll interval for the status sensor.
SCAN_INTERVAL: Final = 60
# Consecutive status polls that must agree the add-on can't reach the HA MCP
# before we raise ISSUE_MCP_UNREACHABLE. The add-on's ha_mcp_connected can read
# false for a single poll on a transient (e.g. a state-free chat turn just after a
# restart) even though MCP works; debouncing across polls stops that flap from
# flashing a scary repair. At SCAN_INTERVAL each extra count is ~1 min of delay
# for a genuine outage, so keep this small. Only the connected-signal path is
# debounced — a genuinely-unloaded mcp_server (a hard local fact) still fires now.
MCP_UNREACHABLE_DEBOUNCE_POLLS: Final = 2
# The usage report is cached by the add-on and heavy to build, so poll it slowly
# (contract §3a: no more than ~every 5 minutes).
USAGE_SCAN_INTERVAL: Final = 300

# --- Services ---------------------------------------------------------------
SERVICE_ASK: Final = "ask"
SERVICE_SETUP_VOICE: Final = "setup_voice"
ATTR_CONFIG_ENTRY: Final = "config_entry"
ATTR_PROMPT: Final = "prompt"
ATTR_MODE: Final = "mode"
ATTR_INTENTS: Final = "intents"
ATTR_NOTIFY: Final = "notify"
ATTR_LANGUAGE: Final = "language"
ATTR_STT_MODEL: Final = "stt_model"
ATTR_TTS_VOICE: Final = "tts_voice"
ATTR_PIPELINE_NAME: Final = "pipeline_name"

# Event fired after a confirmed write executes (for automations/logbook).
EVENT_ACTION_EXECUTED: Final = f"{DOMAIN}_action_executed"

# --- Repair issues ----------------------------------------------------------
ISSUE_ADDON_NOT_RUNNING: Final = "addon_not_running"
ISSUE_ADDON_NOT_INSTALLED: Final = "addon_not_installed"
# Health-check issues: the chat can reach the add-on but can't see/act on the home.
ISSUE_NOT_LOGGED_IN: Final = "not_logged_in"
ISSUE_NO_HA_TOKEN: Final = "no_ha_token"
ISSUE_MCP_UNREACHABLE: Final = "mcp_unreachable"
ISSUE_NO_EXPOSED_ENTITIES: Final = "no_exposed_entities"
# Camera vision is on but no camera is exposed to Assist, so the feature is inert
# (HA hides cameras from Assist by default as security devices). Independent advisory.
ISSUE_CAMERA_VISION_NO_CAMERAS: Final = "camera_vision_no_cameras"
# Health issues cleared together on unload.
HEALTH_ISSUES: Final = (
    ISSUE_NOT_LOGGED_IN,
    ISSUE_NO_HA_TOKEN,
    ISSUE_MCP_UNREACHABLE,
    ISSUE_NO_EXPOSED_ENTITIES,
    ISSUE_CAMERA_VISION_NO_CAMERAS,
)

# The Model Context Protocol Server integration Claude reads the home through.
MCP_SERVER_DOMAIN: Final = "mcp_server"
# The Assist assistant key entity exposure is scoped to.
ASSIST_ASSISTANT: Final = "conversation"
# Tiny read used to populate ha_mcp_connected on an explicit health probe.
HEALTH_PROBE_PROMPT: Final = "ping"

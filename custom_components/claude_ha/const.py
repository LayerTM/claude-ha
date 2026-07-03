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

# Default prompt-server port (contract §4; bound 0.0.0.0, not published to host).
DEFAULT_PORT: Final = 8126

# The add-on option that holds the shared bearer token in the discovery-less
# fallback path (contract §1).
ADDON_OPTION_API_TOKEN: Final = "api_token"

# --- Add-on HTTP contract (see .research/CONTRACT.md) -----------------------
API_PROMPT: Final = "/api/prompt"
API_STATUS: Final = "/api/status"

HEADER_CALLER: Final = "X-Claude-Caller"

# Request modes for POST /api/prompt.
MODE_READ: Final = "read"
MODE_WRITE: Final = "write"
MODES: Final = (MODE_READ, MODE_WRITE)

# Contract §2: `mode:"write"` MUST carry the user-confirmed proposal intents
# (echoed verbatim from a prior read-mode proposal), at most this many, and they
# are forbidden for `mode:"read"`.
MAX_WRITE_INTENTS: Final = 5

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

# GET /api/status 200-response keys.
STATUS_READY: Final = "ready"
STATUS_VERSION: Final = "version"
STATUS_CLAUDE_VERSION: Final = "claude_version"
STATUS_MODEL: Final = "model"

# --- Timings ----------------------------------------------------------------
# How long the integration waits for a single Claude answer (a full agentic run
# on the add-on side can take a while).
REQUEST_TIMEOUT: Final = 120
# Shorter timeout for the lightweight status poll.
STATUS_TIMEOUT: Final = 15
# Coordinator poll interval for the status sensor.
SCAN_INTERVAL: Final = 60

# --- Services ---------------------------------------------------------------
SERVICE_ASK: Final = "ask"
ATTR_CONFIG_ENTRY: Final = "config_entry"
ATTR_PROMPT: Final = "prompt"
ATTR_MODE: Final = "mode"
ATTR_INTENTS: Final = "intents"

# --- Repair issues ----------------------------------------------------------
ISSUE_ADDON_NOT_RUNNING: Final = "addon_not_running"
ISSUE_ADDON_NOT_INSTALLED: Final = "addon_not_installed"

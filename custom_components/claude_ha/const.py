"""Constants for the Claude for Home Assistant integration."""

from __future__ import annotations

import logging

DOMAIN = "claude_ha"
LOGGER = logging.getLogger(__package__)

# The companion add-on's Supervisor slug is repository-prefixed and therefore
# varies per install (e.g. "abc123de_claude-code", "local_claude-code"), so it
# is resolved at runtime — from the discovery payload or by matching this
# suffix against installed add-ons — never hardcoded.
ADDON_SLUG_SUFFIX = "_claude-code"
ADDON_NAME = "Claude Code"

CONF_ADDON_SLUG = "addon_slug"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_TOKEN = "token"
CONF_USE_ADDON = "use_addon"

# How long the integration waits for a single Claude answer.
REQUEST_TIMEOUT = 120

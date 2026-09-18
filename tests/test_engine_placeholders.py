"""Golden-text proof: a Claude entry's rendered strings are unchanged.

Part A replaced literal "Claude"/"Claude Code" text with ``{engine}``/``{addon}``
placeholders throughout ``strings.json``/``translations/en.json``. This captures
the exact English text as it read before that conversion (PR #56 / v1.12.0) and
asserts that rendering the NEW template with the Claude engine's own values
reproduces it byte for byte, for every string the conversion touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.claude_ha.const import DOMAIN
from custom_components.claude_ha.engines import CLAUDE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import translation

_PACKAGE = Path(__file__).parents[1] / "custom_components" / "claude_ha"

# strings.json keys whose value legitimately differs from translations/en.json:
# these use a "[%key:...%]" indirection to a Home Assistant common string, which
# translations/en.json holds resolved instead. Predates this PR (base 43fd6c4).
_KNOWN_VALUE_MISMATCHES = {
    "config.abort.already_configured",
    "config.abort.cannot_connect",
}


def _flatten(value: object, prefix: str = "") -> dict[str, str]:
    """Dotted-key -> value for every string leaf in a nested translation dict."""
    flat: dict[str, str] = {}
    if isinstance(value, dict):
        for key, sub in value.items():
            flat.update(_flatten(sub, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, str):
        flat[prefix] = value
    return flat


def test_strings_json_matches_translations_en() -> None:
    """strings.json and translations/en.json agree on every value but two.

    Both files already carry the same 149 keys (hassfest checks that in CI),
    but nothing compares their *values* -- so a value-only drift in either
    file would pass every other test undetected.
    """
    strings = _flatten(json.loads((_PACKAGE / "strings.json").read_text()))
    english = _flatten(json.loads((_PACKAGE / "translations" / "en.json").read_text()))
    assert strings.keys() == english.keys()
    mismatches = {key for key in strings if strings[key] != english[key]}
    assert mismatches == _KNOWN_VALUE_MISMATCHES


# The exact literal English text of every config.step/progress/abort string this
# PR converted to a placeholder, captured before the conversion.
_CONFIG_OLD = {
    "component.claude_ha.config.step.on_supervisor.title": "Set up Claude",
    "component.claude_ha.config.step.on_supervisor.description": (
        "Claude for Home Assistant runs through the Claude Code add-on. "
        "Do you want to set it up using the add-on?"
    ),
    "component.claude_ha.config.step.on_supervisor.data.use_addon": (
        "Use the Claude Code add-on"
    ),
    "component.claude_ha.config.step.on_supervisor.data_description.use_addon": (
        "The add-on runs Claude on your Home Assistant host and is required "
        "by this integration."
    ),
    "component.claude_ha.config.step.pick_addon.title": (
        "Choose the Claude Code add-on"
    ),
    "component.claude_ha.config.step.pick_addon.description": (
        "More than one Claude Code add-on is available. Choose the one to set up."
    ),
    "component.claude_ha.config.step.hassio_confirm.title": (
        "Set up Claude with the Claude Code add-on"
    ),
    "component.claude_ha.config.step.hassio_confirm.description": (
        "Do you want to set up Claude using the discovered Claude Code add-on?"
    ),
    "component.claude_ha.config.step.install_addon.title": (
        "Installing the Claude Code add-on"
    ),
    "component.claude_ha.config.step.start_addon.title": (
        "Starting the Claude Code add-on"
    ),
    "component.claude_ha.config.progress.install_addon": (
        "Please wait while the Claude Code add-on is installed. "
        "This can take several minutes."
    ),
    "component.claude_ha.config.progress.start_addon": (
        "Please wait while the Claude Code add-on starts."
    ),
    "component.claude_ha.config.abort.addon_required": (
        "The Claude Code add-on is required to use this integration."
    ),
    "component.claude_ha.config.abort.addon_info_failed": (
        "Failed to get the Claude Code add-on info."
    ),
    "component.claude_ha.config.abort.addon_install_failed": (
        "Failed to install the Claude Code add-on."
    ),
    "component.claude_ha.config.abort.addon_start_failed": (
        "Failed to start the Claude Code add-on."
    ),
    "component.claude_ha.config.abort.addon_get_discovery_info_failed": (
        "Failed to get the Claude Code add-on connection details."
    ),
}

# The exact literal English text of every issues.* string this PR converted,
# captured before the conversion.
_ISSUES_OLD = {
    "component.claude_ha.issues.addon_not_running.title": (
        "The Claude Code add-on is not running"
    ),
    "component.claude_ha.issues.addon_not_running.fix_flow.step.confirm.title": (
        "Start the Claude Code add-on"
    ),
    "component.claude_ha.issues.addon_not_running.fix_flow.step.confirm.description": (
        "The Claude Code add-on is installed but not running, so Claude is "
        "unavailable. Do you want to start it now?"
    ),
    "component.claude_ha.issues.addon_not_running.fix_flow.abort.start_failed": (
        "Failed to start the Claude Code add-on. Check the add-on logs and try again."
    ),
    "component.claude_ha.issues.addon_not_installed.title": (
        "The Claude Code add-on is not installed"
    ),
    "component.claude_ha.issues.addon_not_installed.description": (
        "Claude is unavailable because the Claude Code add-on is not installed. "
        "Home Assistant is trying to install it; check the add-on for progress."
    ),
    "component.claude_ha.issues.not_logged_in.title": "Claude isn't logged in",
    "component.claude_ha.issues.not_logged_in.description": (
        "The Claude Code add-on is running but Claude isn't authenticated, so "
        "chat can't answer. Open the Claude Code add-on and run `claude` to log "
        "in (or set an OAuth token in the add-on options), then check again."
    ),
    "component.claude_ha.issues.no_ha_token.title": (
        "Claude has no token to read your home"
    ),
    "component.claude_ha.issues.no_ha_token.description": (
        "Claude is logged in but has no Home Assistant token, so chat can "
        "answer general questions but can't see or control your home. Set the "
        "'Prompt API HA Token' (or 'HA Token') in the Claude Code add-on "
        "options, then check again."
    ),
    "component.claude_ha.issues.mcp_unreachable.title": (
        "Claude can't reach the Model Context Protocol Server"
    ),
    "component.claude_ha.issues.mcp_unreachable.description": (
        "Claude has a token but the Model Context Protocol Server integration "
        "is missing or unreachable, so chat can't see your home. Add it under "
        "Settings → Devices & services → Add integration → Model Context "
        "Protocol Server, expose entities to Assist, then check again."
    ),
    "component.claude_ha.issues.no_exposed_entities.description": (
        "Claude can reach your home but nothing is exposed to Assist, so "
        "there is nothing for it to see or control. Expose some entities "
        "under Settings → Voice assistants → Expose, then check again."
    ),
    "component.claude_ha.issues.camera_vision_no_cameras.description": (
        "You turned on 'Let Claude look at cameras', but no camera is exposed "
        "to Assist, so Claude can never see one — Home Assistant hides "
        "cameras from Assist by default as security devices. Expose the "
        "camera(s) you want Claude to see under Settings → Voice assistants → "
        "Expose (the high-resolution channel of each camera), then check again."
    ),
}

# The exact literal English text of every options.* string this PR converted,
# captured before the conversion.
_OPTIONS_OLD = {
    "component.claude_ha.options.step.init.data.camera_vision": (
        "Let Claude look at cameras"
    ),
    "component.claude_ha.options.step.init.data_description.auto_execute": (
        "When on, Claude carries out low-risk requests from chat immediately "
        "and only asks you to confirm important ones. When off, every change "
        "is confirmed first."
    ),
    "component.claude_ha.options.step.init.data_description.camera_vision": (
        "When on, a clearly visual question (e.g. 'who's at the door?') may "
        "send one snapshot of an Assist-exposed camera to Claude. Off by "
        "default; snapshots are more sensitive than state."
    ),
}

# The exact literal English text of every exceptions.* string this PR converted,
# captured before the conversion.
_EXCEPTIONS_OLD = {
    "component.claude_ha.exceptions.addon_not_ready.message": (
        "The Claude Code add-on is not ready yet."
    ),
    "component.claude_ha.exceptions.addon_info_failed.message": (
        "Failed to get the Claude Code add-on info."
    ),
    "component.claude_ha.exceptions.addon_not_installed.message": (
        "The Claude Code add-on is being installed."
    ),
    "component.claude_ha.exceptions.addon_not_running.message": (
        "The Claude Code add-on is not running yet."
    ),
}

_PLACEHOLDERS = {"engine": CLAUDE.name, "addon": CLAUDE.addon_name}


async def test_config_strings_render_unchanged_for_claude(hass: HomeAssistant) -> None:
    """Every touched config.* string renders the same text for the Claude row."""
    rendered = await translation.async_get_translations(
        hass, "en", "config", {DOMAIN}, config_flow=True
    )
    for key, old_text in _CONFIG_OLD.items():
        assert rendered[key].format(**_PLACEHOLDERS) == old_text, key


async def test_issue_strings_render_unchanged_for_claude(hass: HomeAssistant) -> None:
    """Every touched issues.* string renders the same text for the Claude row."""
    rendered = await translation.async_get_translations(hass, "en", "issues", {DOMAIN})
    for key, old_text in _ISSUES_OLD.items():
        assert rendered[key].format(**_PLACEHOLDERS) == old_text, key


async def test_options_strings_render_unchanged_for_claude(hass: HomeAssistant) -> None:
    """Every touched options.* string renders the same text for the Claude row."""
    rendered = await translation.async_get_translations(hass, "en", "options", {DOMAIN})
    for key, old_text in _OPTIONS_OLD.items():
        assert rendered[key].format(**_PLACEHOLDERS) == old_text, key


async def test_exception_strings_render_unchanged_for_claude(
    hass: HomeAssistant,
) -> None:
    """Every touched exceptions.* string renders the same text for the Claude row."""
    rendered = await translation.async_get_translations(
        hass, "en", "exceptions", {DOMAIN}
    )
    for key, old_text in _EXCEPTIONS_OLD.items():
        assert rendered[key].format(**_PLACEHOLDERS) == old_text, key


async def test_new_config_strings_fill_every_placeholder(hass: HomeAssistant) -> None:
    """The new engine/add_repository steps and the two new aborts all render."""
    rendered = await translation.async_get_translations(
        hass, "en", "config", {DOMAIN}, config_flow=True
    )
    placeholders = {**_PLACEHOLDERS, "repository_url": CLAUDE.repository_url}
    new_keys = [
        "component.claude_ha.config.step.engine.title",
        "component.claude_ha.config.step.engine.description",
        "component.claude_ha.config.step.engine.data.engine",
        "component.claude_ha.config.step.add_repository.title",
        "component.claude_ha.config.step.add_repository.description",
        "component.claude_ha.config.step.add_repository.data.confirm",
        "component.claude_ha.config.step.add_repository.data_description.confirm",
        "component.claude_ha.config.abort.repository_not_added",
        "component.claude_ha.config.abort.addon_not_found",
        "component.claude_ha.config.abort.not_hassio",
    ]
    for key in new_keys:
        assert rendered[key].format(**placeholders)
    # The add_repository step actually shows engine/addon/repository_url.
    assert CLAUDE.repository_url in rendered[
        "component.claude_ha.config.step.add_repository.description"
    ].format(**placeholders)
    # not_hassio deliberately names no specific product.
    assert "Claude" not in rendered["component.claude_ha.config.abort.not_hassio"]


async def test_engine_mismatch_issue_names_both_engines(hass: HomeAssistant) -> None:
    """engine_mismatch now distinguishes the entry's engine from the reported one."""
    rendered = await translation.async_get_translations(hass, "en", "issues", {DOMAIN})
    text = rendered["component.claude_ha.issues.engine_mismatch.description"].format(
        engine="Claude", reported_engine="codex"
    )
    assert "Claude" in text
    assert "codex" in text

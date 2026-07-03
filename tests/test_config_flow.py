"""Tests for the Claude config flow (targets 100% coverage)."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.claude_ha.const import (
    CONF_ADDON_SLUG,
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    CONF_USE_ADDON,
    DOMAIN,
)
from homeassistant.components.hassio import AddonError, AddonState
from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .conftest import (
    TEST_BASE_URL,
    TEST_HOST,
    TEST_PORT,
    TEST_SLUG,
    TEST_TOKEN,
    make_addon_info,
)


@pytest.fixture
def mock_on_supervisor() -> Generator[None]:
    """Report that HA runs on the Supervisor and the add-on slug resolves."""
    with (
        patch("custom_components.claude_ha.config_flow.is_hassio", return_value=True),
        patch(
            "custom_components.claude_ha.config_flow.async_resolve_addon_slug",
            return_value=TEST_SLUG,
        ),
    ):
        yield


def _discovery_info(
    token: str = TEST_TOKEN, slug: str = TEST_SLUG
) -> HassioServiceInfo:
    return HassioServiceInfo(
        config={CONF_HOST: TEST_HOST, CONF_PORT: TEST_PORT, CONF_TOKEN: token},
        name="Claude Code",
        slug=slug,
        uuid="1234",
    )


async def test_user_flow_not_hassio(hass: HomeAssistant) -> None:
    """Without the Supervisor, the flow aborts."""
    with patch("custom_components.claude_ha.config_flow.is_hassio", return_value=False):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_hassio"


async def test_user_flow_addon_not_found(hass: HomeAssistant) -> None:
    """If the add-on cannot be located, the flow aborts."""
    with (
        patch("custom_components.claude_ha.config_flow.is_hassio", return_value=True),
        patch(
            "custom_components.claude_ha.config_flow.async_resolve_addon_slug",
            return_value=None,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "addon_not_found"


async def test_user_flow_addon_running(
    hass: HomeAssistant,
    mock_on_supervisor: None,
    mock_addon_manager: MagicMock,
    mock_status: None,
) -> None:
    """Happy path: the add-on is already running."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "on_supervisor"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_HOST: TEST_HOST,
        CONF_PORT: TEST_PORT,
        CONF_TOKEN: TEST_TOKEN,
        CONF_ADDON_SLUG: TEST_SLUG,
    }
    assert result["result"].unique_id == TEST_SLUG


async def test_user_flow_addon_required(
    hass: HomeAssistant, mock_on_supervisor: None, mock_addon_manager: MagicMock
) -> None:
    """Declining the add-on aborts the flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: False}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "addon_required"


async def test_user_flow_addon_info_failed(
    hass: HomeAssistant, mock_on_supervisor: None, mock_addon_manager: MagicMock
) -> None:
    """A Supervisor error while reading add-on info aborts."""
    mock_addon_manager.async_get_addon_info.side_effect = AddonError("boom")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "addon_info_failed"


async def _advance_progress(hass: HomeAssistant, result: dict) -> dict:
    """Drive a progress flow to its terminal result.

    Instant mocks let ``async_block_till_done`` chain every progress step in one
    pass, after which the flow is gone; a slower mock pauses at each
    ``SHOW_PROGRESS`` and needs another ``async_configure``. This tolerates both.
    """
    from homeassistant.data_entry_flow import UnknownFlow

    for _ in range(8):
        if result["type"] not in (
            FlowResultType.SHOW_PROGRESS,
            FlowResultType.SHOW_PROGRESS_DONE,
        ):
            return result
        await hass.async_block_till_done()
        try:
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        except UnknownFlow:
            break
    return result


async def test_user_flow_installs_and_starts(
    hass: HomeAssistant,
    mock_on_supervisor: None,
    mock_addon_manager: MagicMock,
    mock_status: None,
) -> None:
    """A not-installed add-on is installed, started, then configured."""
    mock_addon_manager.async_get_addon_info.return_value = make_addon_info(
        AddonState.NOT_INSTALLED
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: True}
    )
    result = await _advance_progress(hass, result)

    mock_addon_manager.async_schedule_install_addon.assert_called_once()
    mock_addon_manager.async_schedule_start_addon.assert_called_once()
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_user_flow_starts_stopped_addon(
    hass: HomeAssistant,
    mock_on_supervisor: None,
    mock_addon_manager: MagicMock,
    mock_status: None,
) -> None:
    """A stopped add-on is started, then configured."""
    mock_addon_manager.async_get_addon_info.return_value = make_addon_info(
        AddonState.NOT_RUNNING
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: True}
    )
    result = await _advance_progress(hass, result)

    mock_addon_manager.async_schedule_start_addon.assert_called_once()
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_user_flow_install_fails(
    hass: HomeAssistant, mock_on_supervisor: None, mock_addon_manager: MagicMock
) -> None:
    """A failed install aborts."""
    mock_addon_manager.async_get_addon_info.return_value = make_addon_info(
        AddonState.NOT_INSTALLED
    )
    mock_addon_manager.async_schedule_install_addon.side_effect = AddonError("nope")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: True}
    )
    result = await _advance_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "addon_install_failed"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_user_flow_start_fails(
    hass: HomeAssistant, mock_on_supervisor: None, mock_addon_manager: MagicMock
) -> None:
    """A failed start aborts."""
    mock_addon_manager.async_get_addon_info.return_value = make_addon_info(
        AddonState.NOT_RUNNING
    )
    mock_addon_manager.async_schedule_start_addon.side_effect = AddonError("nope")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: True}
    )
    result = await _advance_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "addon_start_failed"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_user_flow_cannot_connect(
    hass: HomeAssistant,
    mock_on_supervisor: None,
    mock_addon_manager: MagicMock,
    aioclient_mock,
) -> None:
    """A running add-on that fails the status check aborts."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", status=500)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_finish_options_fallback(
    hass: HomeAssistant,
    mock_on_supervisor: None,
    mock_addon_manager: MagicMock,
    mock_status: None,
) -> None:
    """When discovery info is empty, host/port/token come from add-on options."""
    mock_addon_manager.async_get_addon_discovery_info.return_value = {}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == TEST_TOKEN


async def test_finish_discovery_info_failed(
    hass: HomeAssistant, mock_on_supervisor: None, mock_addon_manager: MagicMock
) -> None:
    """When neither discovery nor options provide a token, the flow aborts."""
    mock_addon_manager.async_get_addon_discovery_info.return_value = {}
    mock_addon_manager.async_get_addon_info.return_value = make_addon_info()
    mock_addon_manager.async_get_addon_info.return_value.options.clear()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "addon_get_discovery_info_failed"


async def test_finish_discovery_info_addon_error(
    hass: HomeAssistant, mock_on_supervisor: None, mock_addon_manager: MagicMock
) -> None:
    """A Supervisor error during the options fallback aborts."""
    mock_addon_manager.async_get_addon_discovery_info.side_effect = AddonError("x")
    mock_addon_manager.async_get_addon_info.side_effect = [
        make_addon_info(),
        AddonError("x"),
    ]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "addon_get_discovery_info_failed"


async def test_discovery_flow(
    hass: HomeAssistant,
    mock_on_supervisor: None,
    mock_addon_manager: MagicMock,
    mock_status: None,
) -> None:
    """Add-on discovery leads to a confirm step and entry creation."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_discovery_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == TEST_TOKEN
    assert result["result"].unique_id == TEST_SLUG


async def test_discovery_not_claude_addon(hass: HomeAssistant) -> None:
    """Discovery of an unrelated add-on aborts."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=_discovery_info(slug="other_addon"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_claude_addon"


async def test_discovery_updates_existing_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A second discovery updates the token on the existing entry."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=_discovery_info(token="rotated-token"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config_entry.data[CONF_TOKEN] == "rotated-token"


async def test_user_flow_already_configured(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_on_supervisor: None,
    mock_addon_manager: MagicMock,
) -> None:
    """A second user-initiated setup aborts on the existing unique id."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USE_ADDON: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

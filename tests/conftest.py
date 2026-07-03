"""Fixtures for the Claude for Home Assistant tests."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.const import (
    CONF_ADDON_SLUG,
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    DOMAIN,
)
from homeassistant.components.hassio import AddonInfo, AddonState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

TEST_HOST = "abcd1234-claude-code"
TEST_PORT = 8126
TEST_TOKEN = "s3cr3t-bearer-token"
TEST_SLUG = "abcd1234_claude-code"
TEST_BASE_URL = f"http://{TEST_HOST}:{TEST_PORT}"

STATUS_PAYLOAD = {
    "ready": True,
    "version": "1.7.0",
    "claude_version": "2.0.1",
    "model": "claude-sonnet-4-6",
    "ha_mcp": True,
}
PROMPT_PAYLOAD = {
    "text": "The living room is 21 °C.",
    "proposal": None,
    "tools_used": ["mcp__ha__GetLiveContext"],
    "truncated": False,
}
USAGE_PAYLOAD = {
    "projects": "/data/transcripts",
    "window_days": 7,
    "tokens": {
        "today": {"input": 1000, "output": 500, "cache_read": 0, "cache_write": 0},
        "recent": {"input": 8000, "output": 4000, "cache_read": 0, "cache_write": 0},
        "all_time": {
            "input": 90000,
            "output": 40000,
            "cache_read": 0,
            "cache_write": 0,
        },
    },
    "by_model_recent": {
        "claude-sonnet-4-6": {
            "input": 8000,
            "output": 4000,
            "cache_read": 0,
            "cache_write": 0,
        }
    },
    "messages": {"today": 12, "recent": 90, "all_time": 1000},
    "prompt_api_cost_usd": {"today": 0.12, "total": 1.23},
    "generated_at": "2026-07-03T12:00:00Z",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture(autouse=True)
async def setup_homeassistant(hass: HomeAssistant) -> None:
    """Set up the base ``homeassistant`` integration.

    The ``conversation`` dependency's default agent needs the exposed-entities
    store, which the core integration initializes.
    """
    await async_setup_component(hass, "homeassistant", {})


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a configured (discovery-provisioned) config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Claude Code",
        unique_id=TEST_SLUG,
        data={
            CONF_HOST: TEST_HOST,
            CONF_PORT: TEST_PORT,
            CONF_TOKEN: TEST_TOKEN,
            CONF_ADDON_SLUG: TEST_SLUG,
        },
    )


@pytest.fixture
def mock_status(aioclient_mock: AiohttpClientMocker) -> None:
    """Mock a healthy GET /api/status and GET /api/usage."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json=STATUS_PAYLOAD)
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)


@pytest.fixture
def mock_prompt(aioclient_mock: AiohttpClientMocker) -> None:
    """Mock a POST /api/prompt with a plain answer."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", json=PROMPT_PAYLOAD)


def make_addon_info(state: AddonState = AddonState.RUNNING) -> AddonInfo:
    """Build an AddonInfo in the requested state."""
    return AddonInfo(
        available=True,
        hostname=TEST_HOST,
        options={"api_token": TEST_TOKEN, "api_port": TEST_PORT},
        state=state,
        update_available=False,
        version="1.2.3",
    )


def _done_future(*_args: Any, **_kwargs: Any) -> asyncio.Future[None]:
    """Return a future that resolves on the next loop tick.

    The real ``async_schedule_*`` methods synchronously return an
    ``asyncio.Task``; the config flow awaits it while setup fires it and forgets,
    so the mock must be awaitable AND safe to discard. Resolving on the next tick
    (rather than immediately) forces the awaiting task to suspend, so the config
    flow's ``async_show_progress`` branch is exercised instead of being skipped by
    eager task execution.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    loop.call_soon(future.set_result, None)
    return future


@pytest.fixture
def mock_addon_manager() -> Generator[MagicMock]:
    """Patch the AddonManager factory used by the config flow and setup."""
    manager = MagicMock()
    manager.task_in_progress.return_value = False
    manager.async_get_addon_info = AsyncMock(return_value=make_addon_info())
    manager.async_get_addon_discovery_info = AsyncMock(
        return_value={
            CONF_HOST: TEST_HOST,
            CONF_PORT: TEST_PORT,
            CONF_TOKEN: TEST_TOKEN,
        }
    )
    manager.async_schedule_install_addon = MagicMock(side_effect=_done_future)
    manager.async_schedule_start_addon = MagicMock(side_effect=_done_future)
    manager.async_schedule_install_setup_addon = MagicMock(side_effect=_done_future)
    manager.async_start_addon = AsyncMock()

    with (
        patch(
            "custom_components.claude_ha.config_flow.get_addon_manager",
            return_value=manager,
        ),
        patch(
            "custom_components.claude_ha.addon.get_addon_manager",
            return_value=manager,
        ),
        patch(
            "custom_components.claude_ha.get_addon_manager",
            return_value=manager,
        ),
        patch(
            "custom_components.claude_ha.repairs.get_addon_manager",
            return_value=manager,
        ),
    ):
        yield manager


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Stub async_setup_entry so config-flow tests stop at entry creation."""
    with patch(
        "custom_components.claude_ha.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up a config entry, waiting for it to settle."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

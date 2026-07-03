"""Tests for add-on slug resolution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.claude_ha.addon import (
    async_resolve_addon_slug,
    get_addon_manager,
)
from homeassistant.core import HomeAssistant

from .conftest import TEST_SLUG


def test_get_addon_manager_is_cached(hass: HomeAssistant) -> None:
    """The manager factory constructs one manager per slug and caches it."""
    with patch("custom_components.claude_ha.addon.AddonManager") as mock_manager_cls:
        first = get_addon_manager(hass, TEST_SLUG)
        second = get_addon_manager(hass, TEST_SLUG)
    assert first is second
    mock_manager_cls.assert_called_once()


async def test_resolve_from_installed(hass: HomeAssistant) -> None:
    """An installed add-on is matched by slug suffix."""
    with patch(
        "custom_components.claude_ha.addon.get_addons_info",
        return_value={"other_addon": {}, TEST_SLUG: {}},
    ):
        assert await async_resolve_addon_slug(hass) == TEST_SLUG


async def test_resolve_from_store(hass: HomeAssistant) -> None:
    """A not-installed store add-on is matched when nothing is installed."""
    store_addon = MagicMock(slug=TEST_SLUG, installed=False)
    client = MagicMock()
    client.store.addons_list = _async_return([store_addon])
    with (
        patch("custom_components.claude_ha.addon.get_addons_info", return_value={}),
        patch(
            "custom_components.claude_ha.addon.get_supervisor_client",
            return_value=client,
        ),
    ):
        assert await async_resolve_addon_slug(hass) == TEST_SLUG


async def test_resolve_none(hass: HomeAssistant) -> None:
    """Nothing installed and nothing in the store resolves to None."""
    client = MagicMock()
    client.store.addons_list = _async_return([])
    with (
        patch(
            "custom_components.claude_ha.addon.get_addons_info",
            side_effect=RuntimeError("not ready"),
        ),
        patch(
            "custom_components.claude_ha.addon.get_supervisor_client",
            return_value=client,
        ),
    ):
        assert await async_resolve_addon_slug(hass) is None


async def test_resolve_store_error(hass: HomeAssistant) -> None:
    """A store error is treated as 'not found'."""
    client = MagicMock()
    client.store.addons_list = _async_raise(RuntimeError("boom"))
    with (
        patch("custom_components.claude_ha.addon.get_addons_info", return_value={}),
        patch(
            "custom_components.claude_ha.addon.get_supervisor_client",
            return_value=client,
        ),
    ):
        assert await async_resolve_addon_slug(hass) is None


def _async_return(value: object):
    async def _inner() -> object:
        return value

    return _inner


def _async_raise(exc: Exception):
    async def _inner() -> object:
        raise exc

    return _inner

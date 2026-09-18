"""Tests for add-on slug resolution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.claude_ha.addon import (
    async_drop_addon_watch,
    async_find_addon_slugs,
    get_addon_manager,
    get_addon_watch,
)
from custom_components.claude_ha.engines import CLAUDE, Engine
from homeassistant.core import HomeAssistant

from .conftest import TEST_SLUG

OTHER_ENGINE = Engine(
    key="other",
    name="Other",
    addon_name="Other Agent",
    slug_suffix="_other-agent",
    manufacturer="Someone",
    device_model="Other Agent add-on",
    repository_url="https://github.com/LayerTM/OtherInHA",
)
OTHER_SLUG = "xyz_other-agent"


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
        assert await async_find_addon_slugs(hass) == [TEST_SLUG]


async def test_resolve_every_installed_match(hass: HomeAssistant) -> None:
    """Every installed match is returned, sorted, so none is picked silently."""
    with patch(
        "custom_components.claude_ha.addon.get_addons_info",
        return_value={TEST_SLUG: {}, "local_claude-code": {}, "other_addon": {}},
    ):
        assert await async_find_addon_slugs(hass) == [TEST_SLUG, "local_claude-code"]


def _engine_for_slug(slug: str) -> Engine | None:
    """Map a slug to one of two engines, for tests that must tell them apart."""
    if slug.endswith(CLAUDE.slug_suffix):
        return CLAUDE
    if slug.endswith(OTHER_ENGINE.slug_suffix):
        return OTHER_ENGINE
    return None


async def test_resolve_installed_filters_by_engine(hass: HomeAssistant) -> None:
    """Passing ``engine`` returns only that engine's installed add-ons."""
    with (
        patch(
            "custom_components.claude_ha.addon.get_addons_info",
            return_value={TEST_SLUG: {}, OTHER_SLUG: {}},
        ),
        patch("custom_components.claude_ha.addon.engine_for_slug", _engine_for_slug),
    ):
        assert await async_find_addon_slugs(hass, engine=CLAUDE) == [TEST_SLUG]
        assert await async_find_addon_slugs(hass, engine=OTHER_ENGINE) == [OTHER_SLUG]
        assert await async_find_addon_slugs(hass) == sorted([TEST_SLUG, OTHER_SLUG])


async def test_resolve_store_filters_by_engine(hass: HomeAssistant) -> None:
    """Passing ``engine`` returns only that engine's store add-ons."""
    client = MagicMock()
    client.store.addons_list = _async_return(
        [
            MagicMock(slug=TEST_SLUG, installed=False),
            MagicMock(slug=OTHER_SLUG, installed=False),
        ]
    )
    with (
        patch("custom_components.claude_ha.addon.get_addons_info", return_value={}),
        patch(
            "custom_components.claude_ha.addon.get_supervisor_client",
            return_value=client,
        ),
        patch("custom_components.claude_ha.addon.engine_for_slug", _engine_for_slug),
    ):
        assert await async_find_addon_slugs(hass, engine=OTHER_ENGINE) == [OTHER_SLUG]


async def test_resolve_every_store_match(hass: HomeAssistant) -> None:
    """Every not-installed store match is returned, sorted."""
    client = MagicMock()
    client.store.addons_list = _async_return(
        [
            MagicMock(slug="local_claude-code", installed=False),
            MagicMock(slug=TEST_SLUG, installed=False),
            MagicMock(slug="other_claude-code", installed=True),
        ]
    )
    with (
        patch("custom_components.claude_ha.addon.get_addons_info", return_value={}),
        patch(
            "custom_components.claude_ha.addon.get_supervisor_client",
            return_value=client,
        ),
    ):
        assert await async_find_addon_slugs(hass) == [TEST_SLUG, "local_claude-code"]


def test_addon_watch_is_per_entry(hass: HomeAssistant) -> None:
    """Each entry keeps its own watch until it is removed."""
    first = get_addon_watch(hass, "entry-a", TEST_SLUG)
    assert get_addon_watch(hass, "entry-a", TEST_SLUG) is first
    assert get_addon_watch(hass, "entry-b", "local_claude-code") is not first

    async_drop_addon_watch(hass, "entry-a")
    assert get_addon_watch(hass, "entry-a", TEST_SLUG) is not first


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
        assert await async_find_addon_slugs(hass) == [TEST_SLUG]


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
        assert await async_find_addon_slugs(hass) == []


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
        assert await async_find_addon_slugs(hass) == []


def _async_return(value: object):
    async def _inner() -> object:
        return value

    return _inner


def _async_raise(exc: Exception):
    async def _inner() -> object:
        raise exc

    return _inner

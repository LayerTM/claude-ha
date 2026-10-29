"""Tests for one-click voice setup (Whisper + Piper + Assist pipeline)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohasupervisor import SupervisorError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.claude_ha import voice
from custom_components.claude_ha.const import CONF_VOICE_PIPELINE_ID, DOMAIN
from custom_components.claude_ha.engines import engine_for_entry
from custom_components.claude_ha.voice import (
    ASSIST_PIPELINE_DATA,
    PIPER_ADDON_SLUG,
    WHISPER_ADDON_SLUG,
    VoiceSetupResult,
)
from homeassistant.components.hassio import AddonError, AddonState
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .conftest import make_addon_info, setup_integration


@pytest.fixture(autouse=True)
def addon_addresses(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Answer the apps' Supervisor info: no IP unless a test sets one."""
    addresses: dict[str, Any] = {}

    async def _addon_info(slug: str) -> SimpleNamespace:
        error = addresses.get("error")
        if error is not None:
            raise error
        return SimpleNamespace(ip_address=addresses.get(slug))

    client = SimpleNamespace(addons=SimpleNamespace(addon_info=_addon_info))
    monkeypatch.setattr(voice, "get_supervisor_client", lambda _hass: client)
    return addresses


def _register_wyoming(
    hass: HomeAssistant,
    domain: str,
    obj: str,
    slug: str,
    host: str | None = None,
    disabled: bool = False,
) -> None:
    """Register a Wyoming engine of an app as if discovery had created it."""
    entry = MockConfigEntry(
        domain="wyoming",
        data={CONF_HOST: host or slug.replace("_", "-"), CONF_PORT: 10200},
        disabled_by=ConfigEntryDisabler.USER if disabled else None,
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        domain, "wyoming", obj, suggested_object_id=obj, config_entry=entry
    )


def _install_pipeline_store(hass: HomeAssistant, captured: dict[str, Any]) -> None:
    """Install a fake assist_pipeline store that records created pipelines."""

    class _Store:
        def __init__(self) -> None:
            self.data: dict[str, SimpleNamespace] = {}

        async def async_create_item(self, data: dict[str, Any]) -> SimpleNamespace:
            captured.update(data)
            item = SimpleNamespace(id=f"pipeline-{len(self.data) + 1}", **data)
            self.data[item.id] = item
            return item

        async def async_update_item(
            self, item_id: str, updates: dict[str, Any]
        ) -> SimpleNamespace:
            captured.update(updates)
            item = self.data[item_id]
            item.__dict__.update(updates)
            return item

    hass.data[ASSIST_PIPELINE_DATA] = SimpleNamespace(pipeline_store=_Store())


def test_default_voice() -> None:
    """Known languages map to a voice; unknown ones do not."""
    assert voice.default_voice("uk") == "uk_UA-ukrainian_tts-medium"
    assert voice.default_voice("zz") is None


async def test_ensure_addon_installs_configures_starts(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing app is installed, configured and started."""
    manager = MagicMock()
    manager.async_get_addon_info = AsyncMock(
        side_effect=[
            make_addon_info(AddonState.NOT_INSTALLED),
            make_addon_info(AddonState.NOT_RUNNING),
            make_addon_info(AddonState.NOT_RUNNING),
        ]
    )
    manager.async_install_addon = AsyncMock()
    manager.async_set_addon_options = AsyncMock()
    manager.async_start_addon = AsyncMock()
    monkeypatch.setattr(voice, "get_addon_manager", lambda _h, _s, _n: manager)

    await voice._async_ensure_addon(
        hass, WHISPER_ADDON_SLUG, "Whisper", {"model": "small-int8"}
    )

    manager.async_install_addon.assert_awaited_once()
    # The app's own defaults (incl. options it requires) are kept.
    manager.async_set_addon_options.assert_awaited_once_with(
        {**make_addon_info().options, "model": "small-int8"}
    )
    manager.async_start_addon.assert_awaited_once()


async def test_ensure_addon_already_running(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed, running app is only reconfigured, not reinstalled."""
    manager = MagicMock()
    manager.async_get_addon_info = AsyncMock(
        return_value=make_addon_info(AddonState.RUNNING)
    )
    manager.async_install_addon = AsyncMock()
    manager.async_set_addon_options = AsyncMock()
    manager.async_start_addon = AsyncMock()
    monkeypatch.setattr(voice, "get_addon_manager", lambda _h, _s, _n: manager)

    await voice._async_ensure_addon(hass, PIPER_ADDON_SLUG, "Piper", {"voice": "x"})

    manager.async_install_addon.assert_not_awaited()
    manager.async_start_addon.assert_not_awaited()


async def test_ensure_addon_wraps_error(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Supervisor error is surfaced as a translated HomeAssistantError."""
    manager = MagicMock()
    manager.async_get_addon_info = AsyncMock(side_effect=AddonError("boom"))
    monkeypatch.setattr(voice, "get_addon_manager", lambda _h, _s, _n: manager)

    with pytest.raises(HomeAssistantError):
        await voice._async_ensure_addon(hass, WHISPER_ADDON_SLUG, "Whisper", {})


async def test_setup_pipeline_twice_reuses_the_pipeline(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the setup again re-wires the pipeline it made instead of adding one."""
    captured: dict[str, Any] = {}
    _install_pipeline_store(hass, captured)

    async def _fake_ensure(
        _hass: HomeAssistant, slug: str, _name: str, _options: dict[str, Any]
    ) -> None:
        if slug == WHISPER_ADDON_SLUG:
            _register_wyoming(hass, "stt", "faster_whisper", WHISPER_ADDON_SLUG)
        else:
            _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG)

    monkeypatch.setattr(voice, "_async_ensure_addon", _fake_ensure)

    async def _run(
        name: str, piper_voice: str, entity: str, remembered: str | None = None
    ) -> str | None:
        result = await voice.async_setup_voice_pipeline(
            hass,
            entity,
            language="fr",
            whisper_model=None,
            piper_voice=piper_voice,
            pipeline_name=name,
            pipeline_id=remembered,
        )
        return result.pipeline_id

    store = hass.data[ASSIST_PIPELINE_DATA].pipeline_store
    # A pipeline of the user's own, same name and same agent, made by hand.
    store.data["hand-made"] = SimpleNamespace(
        id="hand-made",
        name="Claude (fr)",
        conversation_engine="conversation.claude",
        tts_voice="mine",
    )

    first = await _run("Claude (fr)", "fr_FR-a", "conversation.claude")
    again = await _run("Claude (fr)", "fr_FR-b", "conversation.claude", first)
    gone = await _run("Claude (fr)", "fr_FR-c", "conversation.claude", "deleted")

    assert first != "hand-made"
    assert store.data["hand-made"].tts_voice == "mine"
    assert again == first
    assert store.data[first].tts_voice == "fr_FR-b"
    # A remembered id the user has since deleted is made again, not guessed.
    assert gone not in (first, "hand-made", "deleted")
    assert len(store.data) == 3


async def test_setup_pipeline_full(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engines discovered → an Assist pipeline wired to Claude is created."""
    captured: dict[str, Any] = {}
    _install_pipeline_store(hass, captured)

    async def _fake_ensure(
        _hass: HomeAssistant, slug: str, _name: str, _options: dict[str, Any]
    ) -> None:
        if slug == WHISPER_ADDON_SLUG:
            _register_wyoming(hass, "stt", "faster_whisper", WHISPER_ADDON_SLUG)
        else:
            _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG)

    monkeypatch.setattr(voice, "_async_ensure_addon", _fake_ensure)

    result = await voice.async_setup_voice_pipeline(
        hass,
        "conversation.claude",
        language="uk",
        whisper_model="small-int8",
        piper_voice="uk_UA-x",
        pipeline_name="Claude (uk)",
    )

    assert result.pipeline_id == "pipeline-1"
    assert result.stt_engine == "stt.faster_whisper"
    assert result.tts_engine == "tts.piper"
    assert captured["conversation_engine"] == "conversation.claude"
    assert captured["tts_voice"] == "uk_UA-x"
    assert captured["language"] == "uk"


def _flow(flow_id: str, slug: str) -> dict[str, Any]:
    """Return a pending Wyoming Supervisor-discovery flow for an app slug."""
    return {
        "flow_id": flow_id,
        "context": {
            "source": "hassio",
            "configuration_url": f"homeassistant://hassio/addon/{slug}/info",
        },
    }


@pytest.mark.parametrize(
    ("model", "expected"),
    [(None, {"language": "fr"}), ("tiny", {"language": "fr", "model": "tiny"})],
)
async def test_setup_pipeline_sends_a_model_only_when_given(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
    expected: dict[str, Any],
) -> None:
    """A Whisper model the user chose is kept unless the caller names another."""
    monkeypatch.setattr(voice, "ENGINE_DISCOVERY_TIMEOUT", 0.01)
    sent: dict[str, dict[str, Any]] = {}

    async def _record(
        _hass: HomeAssistant, slug: str, _name: str, options: dict[str, Any]
    ) -> None:
        sent[slug] = options

    monkeypatch.setattr(voice, "_async_ensure_addon", _record)

    await voice.async_setup_voice_pipeline(
        hass,
        "conversation.claude",
        language="fr",
        whisper_model=model,
        piper_voice="fr_FR-x",
        pipeline_name="Claude (fr)",
    )

    assert sent[WHISPER_ADDON_SLUG] == expected
    assert sent[PIPER_ADDON_SLUG] == {"voice": "fr_FR-x"}


async def test_setup_pipeline_confirms_only_its_own_discoveries(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whisper/Piper discovery prompts are answered; anything else is left alone."""
    captured: dict[str, Any] = {}
    _install_pipeline_store(hass, captured)
    listeners: list[Any] = []
    pending: list[dict[str, Any]] = [_flow("left-over", WHISPER_ADDON_SLUG)]
    configured: list[str] = []

    async def _configure(flow_id: str, data: dict[str, Any]) -> None:
        assert data == {}
        configured.append(flow_id)
        if flow_id == "left-over":
            _register_wyoming(hass, "stt", "faster_whisper", WHISPER_ADDON_SLUG)
        elif flow_id == "piper":
            _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG)

    fake = SimpleNamespace(
        async_subscribe_flow=lambda listener: (
            listeners.append(listener) or (lambda: listeners.remove(listener))
        ),
        async_progress_by_handler=lambda *_a, **_k: list(pending),
        async_configure=_configure,
        async_shutdown=MagicMock(),
    )
    monkeypatch.setattr(hass.config_entries, "flow", fake)

    async def _ensure(
        _hass: HomeAssistant, slug: str, _name: str, _options: dict[str, Any]
    ) -> None:
        if slug == PIPER_ADDON_SLUG:
            pending.extend(
                [
                    _flow("piper", PIPER_ADDON_SLUG),
                    _flow("other", "core_other"),
                ]
            )
            for event, flow_id in (
                ("added", "piper"),
                ("added", "other"),
                ("removed", "piper"),
            ):
                listeners[0](event, flow_id)

    monkeypatch.setattr(voice, "_async_ensure_addon", _ensure)

    result = await voice.async_setup_voice_pipeline(
        hass,
        "conversation.claude",
        language="fr",
        whisper_model=None,
        piper_voice="fr_FR-x",
        pipeline_name="Claude (fr)",
    )

    assert result.pipeline_id == "pipeline-1"
    assert sorted(configured) == ["left-over", "piper"]
    assert listeners == []


def _fake_flow_manager(
    monkeypatch: pytest.MonkeyPatch,
    hass: HomeAssistant,
    pending: list[dict[str, Any]],
    configure: Any,
    aborted: list[str] | None = None,
    listeners: list[Any] | None = None,
) -> None:
    """Replace the config-entries flow manager with one holding ``pending``."""
    if aborted is None:
        aborted = []
    if listeners is None:
        listeners = []

    def _subscribe(listener: Any) -> Any:
        listeners.append(listener)
        return lambda: None

    fake = SimpleNamespace(
        async_subscribe_flow=_subscribe,
        async_progress_by_handler=lambda *_a, **_k: list(pending),
        async_configure=configure,
        async_abort=aborted.append,
        async_shutdown=MagicMock(),
    )
    monkeypatch.setattr(hass.config_entries, "flow", fake)


async def _setup_voice(hass: HomeAssistant) -> VoiceSetupResult:
    """Run the voice setup with apps that are already installed and running."""
    return await voice.async_setup_voice_pipeline(
        hass,
        "conversation.claude",
        language="uk",
        whisper_model=None,
        piper_voice="uk_UA-x",
        pipeline_name="Claude (uk)",
    )


async def _noop_ensure(
    _hass: HomeAssistant, _slug: str, _name: str, _options: dict[str, Any]
) -> None:
    return None


async def test_setup_pipeline_reuses_engines_that_already_exist(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second run finds Whisper and Piper already wired and builds the pipeline."""
    monkeypatch.setattr(voice, "ENGINE_DISCOVERY_TIMEOUT", 0.01)
    captured: dict[str, Any] = {}
    _install_pipeline_store(hass, captured)
    _register_wyoming(hass, "stt", "faster_whisper", WHISPER_ADDON_SLUG)
    _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG)
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    result = await _setup_voice(hass)

    assert result == VoiceSetupResult("stt.faster_whisper", "tts.piper", "pipeline-1")


async def test_setup_pipeline_ignores_other_wyoming_engines(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another Wyoming server's engine is never taken for Whisper or Piper."""
    monkeypatch.setattr(voice, "ENGINE_DISCOVERY_TIMEOUT", 0.01)
    _register_wyoming(hass, "stt", "other_stt", "core_other")
    _register_wyoming(hass, "tts", "other_tts", "core_other")
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    assert await _setup_voice(hass) == VoiceSetupResult(None, None, None)


async def test_setup_pipeline_does_not_confirm_a_duplicate_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovery for an app that already has an entry is left unconfirmed."""
    _install_pipeline_store(hass, {})
    _register_wyoming(hass, "stt", "faster_whisper", WHISPER_ADDON_SLUG)
    _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG)
    configured: list[str] = []

    async def _configure(flow_id: str, _data: dict[str, Any]) -> None:
        configured.append(flow_id)

    _fake_flow_manager(
        monkeypatch,
        hass,
        [_flow("stale", PIPER_ADDON_SLUG), _flow("new", WHISPER_ADDON_SLUG)],
        _configure,
    )
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    await _setup_voice(hass)

    assert configured == []


async def test_setup_pipeline_confirms_one_discovery_per_addon(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale and a fresh discovery of one app make one entry, not two."""
    monkeypatch.setattr(voice, "ENGINE_DISCOVERY_TIMEOUT", 0.01)
    configured: list[str] = []

    async def _configure(flow_id: str, _data: dict[str, Any]) -> None:
        configured.append(flow_id)

    _fake_flow_manager(
        monkeypatch,
        hass,
        [_flow("stale", PIPER_ADDON_SLUG), _flow("new", PIPER_ADDON_SLUG)],
        _configure,
    )
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    await _setup_voice(hass)

    assert configured == ["stale"]


async def test_setup_pipeline_retries_a_discovery_that_failed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A discovery that cannot be confirmed does not block the app's next one."""
    monkeypatch.setattr(voice, "ENGINE_DISCOVERY_TIMEOUT", 0.01)
    configured: list[str] = []

    async def _configure(flow_id: str, _data: dict[str, Any]) -> None:
        configured.append(flow_id)
        if flow_id == "gone":
            raise UnknownFlow

    _fake_flow_manager(
        monkeypatch,
        hass,
        [_flow("gone", PIPER_ADDON_SLUG), _flow("new", PIPER_ADDON_SLUG)],
        _configure,
    )
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    await _setup_voice(hass)

    assert configured == ["gone", "new"]
    assert "Could not confirm the Wyoming discovery" in caplog.text


async def test_setup_pipeline_engine_timeout(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the engines never appear, no pipeline is created."""
    monkeypatch.setattr(voice, "ENGINE_DISCOVERY_TIMEOUT", 0.01)

    async def _noop(
        _hass: HomeAssistant, _slug: str, _name: str, _options: dict[str, Any]
    ) -> None:
        return None

    monkeypatch.setattr(voice, "_async_ensure_addon", _noop)

    result = await voice.async_setup_voice_pipeline(
        hass,
        "conversation.claude",
        language="pl",
        whisper_model="small-int8",
        piper_voice="pl_PL-x",
        pipeline_name="Claude (pl)",
    )

    assert result == VoiceSetupResult(None, None, None)


async def test_setup_pipeline_without_assist_pipeline(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engines found but assist_pipeline absent → engines returned, no pipeline."""
    hass.data.pop(ASSIST_PIPELINE_DATA, None)

    async def _fake_ensure(
        _hass: HomeAssistant, slug: str, _name: str, _options: dict[str, Any]
    ) -> None:
        domain = "stt" if slug == WHISPER_ADDON_SLUG else "tts"
        _register_wyoming(hass, domain, f"engine_{domain}", slug)

    monkeypatch.setattr(voice, "_async_ensure_addon", _fake_ensure)

    result = await voice.async_setup_voice_pipeline(
        hass,
        "conversation.claude",
        language="en",
        whisper_model="small-int8",
        piper_voice="en_US-x",
        pipeline_name="Claude (en)",
    )

    assert result.stt_engine is not None
    assert result.pipeline_id is None


async def test_service_setup_voice_reuse_is_not_created(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-wiring the remembered pipeline reports created_pipeline false."""
    await setup_integration(hass, mock_config_entry)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_VOICE_PIPELINE_ID: "pipeline-1"},
    )

    async def _fake_pipeline(
        _hass: HomeAssistant, conv_id: str, **kwargs: Any
    ) -> VoiceSetupResult:
        return VoiceSetupResult("stt.whisper", "tts.piper", "pipeline-1")

    monkeypatch.setattr(
        "custom_components.claude_ha.services.async_setup_voice_pipeline",
        _fake_pipeline,
    )

    response = await hass.services.async_call(
        DOMAIN,
        "setup_voice",
        {"language": "uk"},
        blocking=True,
        return_response=True,
    )
    assert response["created_pipeline"] is False
    assert response["pipeline_id"] == "pipeline-1"


async def test_service_setup_voice_success(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service resolves the Claude agent and returns the setup result."""
    await setup_integration(hass, mock_config_entry)
    captured: dict[str, Any] = {}

    async def _fake_pipeline(
        _hass: HomeAssistant, conv_id: str, **kwargs: Any
    ) -> VoiceSetupResult:
        assert conv_id.startswith("conversation.")
        captured.update(kwargs)
        return VoiceSetupResult("stt.whisper", "tts.piper", "pipeline-1")

    monkeypatch.setattr(
        "custom_components.claude_ha.services.async_setup_voice_pipeline",
        _fake_pipeline,
    )

    response = await hass.services.async_call(
        DOMAIN,
        "setup_voice",
        {"language": "uk"},
        blocking=True,
        return_response=True,
    )
    assert response["created_pipeline"] is True
    assert response["pipeline_id"] == "pipeline-1"
    assert response["tts_voice"] == "uk_UA-ukrainian_tts-medium"
    # The default pipeline name uses the entry's own engine name, not a literal.
    engine = engine_for_entry(mock_config_entry)
    assert engine is not None
    assert captured["pipeline_name"] == f"{engine.name} (uk)"
    # The pipeline is remembered, so the next run re-wires it.
    assert captured["pipeline_id"] is None
    assert mock_config_entry.data[CONF_VOICE_PIPELINE_ID] == "pipeline-1"
    await hass.services.async_call(
        DOMAIN, "setup_voice", {"language": "uk"}, blocking=True, return_response=True
    )
    assert captured["pipeline_id"] == "pipeline-1"


async def test_service_setup_voice_overlapping_calls_make_one_pipeline(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two overlapping runs are serialised: one pipeline, the second re-wires it."""
    await setup_integration(hass, mock_config_entry)
    running = 0
    overlapped = False
    made: list[str] = []
    seen: list[str | None] = []

    async def _fake_pipeline(
        _hass: HomeAssistant, conv_id: str, **kwargs: Any
    ) -> VoiceSetupResult:
        nonlocal running, overlapped
        running += 1
        overlapped = overlapped or running > 1
        seen.append(kwargs["pipeline_id"])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        running -= 1
        if kwargs["pipeline_id"] is None:
            made.append(f"pipeline-{len(made) + 1}")
            return VoiceSetupResult("stt.whisper", "tts.piper", made[-1])
        return VoiceSetupResult("stt.whisper", "tts.piper", kwargs["pipeline_id"])

    monkeypatch.setattr(
        "custom_components.claude_ha.services.async_setup_voice_pipeline",
        _fake_pipeline,
    )

    first, second = await asyncio.gather(
        *(
            hass.services.async_call(
                DOMAIN,
                "setup_voice",
                {"language": "uk"},
                blocking=True,
                return_response=True,
            )
            for _ in range(2)
        )
    )
    assert not overlapped
    assert made == ["pipeline-1"]
    assert seen == [None, "pipeline-1"]
    assert [first["created_pipeline"], second["created_pipeline"]] == [True, False]
    assert first["pipeline_id"] == second["pipeline_id"] == "pipeline-1"
    assert mock_config_entry.data[CONF_VOICE_PIPELINE_ID] == "pipeline-1"


@pytest.mark.parametrize("service", ["ask", "setup_voice"])
async def test_services_answer_with_or_without_a_response(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    service: str,
) -> None:
    """Callers that cannot request a response (MCP, plain actions) may still call."""
    await setup_integration(hass, mock_config_entry)
    assert hass.services.supports_response(DOMAIN, service) is SupportsResponse.OPTIONAL


async def test_ensure_addon_names_third_party_managers(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real manager factory serves the Whisper and Piper slugs (no engine)."""
    for slug, name in ((WHISPER_ADDON_SLUG, "Whisper"), (PIPER_ADDON_SLUG, "Piper")):
        manager = MagicMock()
        manager.async_get_addon_info = AsyncMock(
            return_value=make_addon_info(AddonState.RUNNING)
        )
        manager.async_set_addon_options = AsyncMock()
        created = MagicMock(return_value=manager)
        monkeypatch.setattr("custom_components.claude_ha.addon.AddonManager", created)
        await voice._async_ensure_addon(hass, slug, name, {})
        assert created.call_args.args[2:] == (name, slug)


async def test_service_setup_voice_unknown_language(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """An unknown language with no voice override is rejected."""
    await setup_integration(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "setup_voice",
            {"language": "zz"},
            blocking=True,
            return_response=True,
        )


async def test_service_setup_voice_no_conversation_entity(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the Claude conversation entity can't be resolved, the call is rejected."""
    await setup_integration(hass, mock_config_entry)
    monkeypatch.setattr(
        er.async_get(hass), "async_get_entity_id", lambda *_a, **_k: None
    )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "setup_voice",
            {"language": "uk"},
            blocking=True,
            return_response=True,
        )


async def test_setup_pipeline_dismisses_the_discoveries_it_skips(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovery that is not confirmed is dismissed, so no prompt is left over."""
    monkeypatch.setattr(voice, "ENGINE_DISCOVERY_TIMEOUT", 0.01)
    _register_wyoming(hass, "stt", "faster_whisper", WHISPER_ADDON_SLUG)
    configured: list[str] = []
    aborted: list[str] = []

    async def _configure(flow_id: str, _data: dict[str, Any]) -> None:
        configured.append(flow_id)

    _fake_flow_manager(
        monkeypatch,
        hass,
        [
            _flow("whisper-again", WHISPER_ADDON_SLUG),
            _flow("piper", PIPER_ADDON_SLUG),
            _flow("piper-stale", PIPER_ADDON_SLUG),
        ],
        _configure,
        aborted,
    )
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    await _setup_voice(hass)

    assert configured == ["piper"]
    assert sorted(aborted) == ["piper-stale", "whisper-again"]


async def test_setup_pipeline_dismiss_tolerates_a_vanished_flow(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovery that is gone by the time it is dismissed is not an error."""
    monkeypatch.setattr(voice, "ENGINE_DISCOVERY_TIMEOUT", 0.01)
    _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG)

    def _abort(_flow_id: str) -> None:
        raise UnknownFlow

    _fake_flow_manager(
        monkeypatch, hass, [_flow("piper-again", PIPER_ADDON_SLUG)], AsyncMock()
    )
    monkeypatch.setattr(hass.config_entries.flow, "async_abort", _abort)
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    result = await _setup_voice(hass)

    assert result.tts_engine == "tts.piper"


async def test_setup_pipeline_knows_an_entry_by_the_addon_ip(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    addon_addresses: dict[str, Any],
) -> None:
    """An entry added by hand with the app's IP is the app's, not a stranger."""
    _install_pipeline_store(hass, {})
    addon_addresses[WHISPER_ADDON_SLUG] = "192.0.2.14"
    addon_addresses[PIPER_ADDON_SLUG] = "192.0.2.15"
    _register_wyoming(hass, "stt", "whisper_ip", WHISPER_ADDON_SLUG, host="192.0.2.14")
    _register_wyoming(hass, "tts", "piper_ip", PIPER_ADDON_SLUG, host="192.0.2.15")
    configured: list[str] = []

    async def _configure(flow_id: str, _data: dict[str, Any]) -> None:
        configured.append(flow_id)

    _fake_flow_manager(
        monkeypatch, hass, [_flow("whisper", WHISPER_ADDON_SLUG)], _configure
    )
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    aborted: list[str] = []
    _fake_flow_manager(
        monkeypatch,
        hass,
        [_flow("whisper", WHISPER_ADDON_SLUG)],
        _configure,
        aborted,
    )

    result = await _setup_voice(hass)

    assert result == VoiceSetupResult("stt.whisper_ip", "tts.piper_ip", "pipeline-1")
    assert configured == []
    assert aborted == ["whisper"]


async def test_setup_pipeline_without_supervisor_info_uses_the_hostname(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    addon_addresses: dict[str, Any],
) -> None:
    """When Supervisor cannot say an app's IP, its hostname still matches."""
    _install_pipeline_store(hass, {})
    addon_addresses["error"] = SupervisorError("unavailable")
    _register_wyoming(hass, "stt", "faster_whisper", WHISPER_ADDON_SLUG)
    _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG)
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    result = await _setup_voice(hass)

    assert result.pipeline_id == "pipeline-1"


async def test_setup_pipeline_says_when_an_entry_is_disabled(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled Wyoming entry is reported at once instead of a 90 s wait."""
    _register_wyoming(hass, "stt", "faster_whisper", WHISPER_ADDON_SLUG, disabled=True)
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    with pytest.raises(HomeAssistantError) as err:
        await _setup_voice(hass)

    assert err.value.translation_key == "voice_engine_disabled"
    assert err.value.translation_placeholders == {"addon": "Whisper"}


async def test_setup_pipeline_checks_a_discovery_made_by_starting_the_addon(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    addon_addresses: dict[str, Any],
) -> None:
    """A discovery that appears while the app starts is checked by its IP too."""
    _install_pipeline_store(hass, {})
    addon_addresses[WHISPER_ADDON_SLUG] = "192.0.2.14"
    _register_wyoming(hass, "stt", "whisper_ip", WHISPER_ADDON_SLUG, host="192.0.2.14")
    _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG)
    pending: list[dict[str, Any]] = []
    listeners: list[Any] = []
    configured: list[str] = []
    aborted: list[str] = []

    async def _configure(flow_id: str, _data: dict[str, Any]) -> None:
        configured.append(flow_id)

    async def _ensure_starts_and_discovers(
        _hass: HomeAssistant, slug: str, _name: str, _options: dict[str, Any]
    ) -> None:
        if slug == WHISPER_ADDON_SLUG:
            pending.append(_flow("on-start", WHISPER_ADDON_SLUG))
            for listener in listeners:
                listener("added", "on-start")

    _fake_flow_manager(monkeypatch, hass, pending, _configure, aborted, listeners)
    monkeypatch.setattr(voice, "_async_ensure_addon", _ensure_starts_and_discovers)

    result = await _setup_voice(hass)

    assert result.stt_engine == "stt.whisper_ip"
    assert configured == []
    assert aborted == ["on-start"]


async def test_setup_pipeline_keeps_a_duplicate_when_the_confirm_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate prompt stays when the claimed one could not be confirmed."""
    monkeypatch.setattr(voice, "ENGINE_DISCOVERY_TIMEOUT", 0.01)
    aborted: list[str] = []

    async def _configure(flow_id: str, _data: dict[str, Any]) -> None:
        raise HomeAssistantError("boom")

    _fake_flow_manager(
        monkeypatch,
        hass,
        [_flow("first", PIPER_ADDON_SLUG), _flow("second", PIPER_ADDON_SLUG)],
        _configure,
        aborted,
    )
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    await _setup_voice(hass)

    assert aborted == []


async def test_setup_pipeline_matches_hosts_regardless_of_case(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand entry spelled "Core-Whisper" is still the app's."""
    _install_pipeline_store(hass, {})
    _register_wyoming(hass, "stt", "whisper", WHISPER_ADDON_SLUG, host="Core-Whisper")
    _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG)
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    result = await _setup_voice(hass)

    assert result.stt_engine == "stt.whisper"


async def test_setup_pipeline_one_disabled_entry_beside_an_enabled_one_waits(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only when every entry of the app is disabled is that reported."""
    _install_pipeline_store(hass, {})
    _register_wyoming(hass, "stt", "old", WHISPER_ADDON_SLUG, disabled=True)
    _register_wyoming(hass, "stt", "faster_whisper", WHISPER_ADDON_SLUG)
    _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG)
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    result = await _setup_voice(hass)

    assert result.pipeline_id == "pipeline-1"


async def test_setup_pipeline_says_when_the_piper_entry_is_disabled(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second engine's disabled entry is reported the same way."""
    _register_wyoming(hass, "stt", "faster_whisper", WHISPER_ADDON_SLUG)
    _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG, disabled=True)
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    with pytest.raises(HomeAssistantError) as err:
        await _setup_voice(hass)

    assert err.value.translation_placeholders == {"addon": "Piper"}


async def test_setup_pipeline_addon_without_an_ip_uses_the_hostname(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    addon_addresses: dict[str, Any],
) -> None:
    """Supervisor reporting no IP leaves the hostname as the only address."""
    _install_pipeline_store(hass, {})
    addon_addresses[WHISPER_ADDON_SLUG] = None
    addon_addresses[PIPER_ADDON_SLUG] = ""
    _register_wyoming(hass, "stt", "faster_whisper", WHISPER_ADDON_SLUG)
    _register_wyoming(hass, "tts", "piper", PIPER_ADDON_SLUG)
    monkeypatch.setattr(voice, "_async_ensure_addon", _noop_ensure)

    assert await voice._async_addon_hosts(hass, WHISPER_ADDON_SLUG) == frozenset(
        {"core-whisper"}
    )
    result = await _setup_voice(hass)

    assert result.pipeline_id == "pipeline-1"

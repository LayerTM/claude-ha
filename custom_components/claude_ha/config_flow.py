"""Config flow for the Claude for Home Assistant integration."""

from __future__ import annotations

import asyncio
from typing import Any

from aiohasupervisor.models import StoreAddRepository
import voluptuous as vol

from homeassistant.components.hassio import (
    AddonError,
    AddonInfo,
    AddonManager,
    AddonState,
    get_supervisor_client,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .addon import async_find_addon_slugs, get_addon_manager
from .api import ClaudeClient, ClaudeEngineMismatchError, ClaudeError
from .const import (
    ADDON_OPTION_API_TOKEN,
    CONF_ADDON_SLUG,
    CONF_AUTO_EXECUTE,
    CONF_CAMERA_VISION,
    CONF_CRITICAL_ENTITIES,
    CONF_ENGINE,
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    CONF_USE_ADDON,
    CONFIG_ENTRY_MINOR_VERSION,
    DEFAULT_PORT,
    DOMAIN,
    LOGGER,
)
from .engines import ENGINES, Engine, engine_for_entry, engine_for_slug

ON_SUPERVISOR_SCHEMA = vol.Schema({vol.Required(CONF_USE_ADDON, default=True): bool})
ADD_REPOSITORY_SCHEMA = vol.Schema({vol.Required("confirm", default=False): bool})

# The add-on slug picked in the "pick_addon" step.
CONF_ADDON = "addon"


class ClaudeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for an AI agent, backed by its companion add-on."""

    VERSION = 1
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ClaudeOptionsFlow:
        """Return the options flow."""
        return ClaudeOptionsFlow()

    def __init__(self) -> None:
        """Init flow state."""
        self._addon_slug: str | None = None
        self._engine_key: str | None = None
        self._discovery: dict[str, Any] | None = None
        self._addon_choices: list[str] = []
        self.install_task: asyncio.Task[None] | None = None
        self.start_task: asyncio.Task[None] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a user-initiated flow. Requires Supervisor + the add-on."""
        if not is_hassio(self.hass):
            return self.async_abort(reason="not_hassio")
        return await self.async_step_engine()

    async def async_step_engine(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which AI agent this entry talks to."""
        if user_input is not None:
            self._engine_key = user_input[CONF_ENGINE]
            return await self.async_step_resolve_engine()
        return self.async_show_form(
            step_id="engine",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ENGINE): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=engine.key, label=engine.name
                                )
                                for engine in ENGINES.values()
                            ]
                        )
                    )
                }
            ),
        )

    async def async_step_resolve_engine(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Resolve the chosen engine's add-on slug, then continue the flow."""
        result = await self._async_resolve_slug()
        if result is not None:
            return result
        return await self.async_step_on_supervisor()

    async def _async_resolve_slug(self) -> ConfigFlowResult | None:
        """Find the chosen engine's add-on slug.

        On success, sets ``self._addon_slug`` and returns ``None`` (the caller
        proceeds); otherwise returns the step the caller should show instead:
        a choice between several candidates, or an offer to add the engine's
        add-on repository when none was found.
        """
        assert self._engine_key is not None
        engine = ENGINES[self._engine_key]
        slugs = await async_find_addon_slugs(self.hass, engine=engine)
        if not slugs:
            return await self.async_step_add_repository()
        if len(slugs) > 1:
            configured = self._async_current_ids(include_ignore=False)
            slugs = [slug for slug in slugs if slug not in configured]
            if not slugs:
                return self.async_abort(reason="already_configured")
            if len(slugs) > 1:
                self._addon_choices = slugs
                return await self.async_step_pick_addon()
        self._addon_slug = slugs[0]
        return None

    async def async_step_add_repository(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to add the chosen engine's add-on repository to the store."""
        engine = self._engine
        if user_input is None:
            return self.async_show_form(
                step_id="add_repository",
                data_schema=ADD_REPOSITORY_SCHEMA,
                description_placeholders={
                    "addon": engine.addon_name,
                    "repository_url": engine.repository_url,
                },
            )
        if not user_input["confirm"]:
            return self.async_abort(reason="repository_not_added")

        try:
            client = get_supervisor_client(self.hass)
            await client.store.add_repository(
                StoreAddRepository(repository=engine.repository_url)
            )
            await client.store.reload()
        except Exception as err:  # noqa: BLE001 - Supervisor store may be unavailable
            LOGGER.error(
                "Failed to add the %s add-on repository: %s", engine.addon_name, err
            )
            return self.async_abort(reason="addon_get_discovery_info_failed")

        result = await self._async_resolve_slug()
        if result is None:
            return await self.async_step_on_supervisor()
        if result["step_id"] == "add_repository":
            return self.async_abort(reason="addon_not_found")
        return result

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Handle add-on discovery (the add-on advertised host/port/token)."""
        if engine_for_slug(discovery_info.slug) is None:
            return self.async_abort(reason="unsupported_addon")

        config = discovery_info.config
        await self.async_set_unique_id(discovery_info.slug)
        self._abort_if_unique_id_configured(
            updates={
                CONF_HOST: config[CONF_HOST],
                CONF_PORT: config[CONF_PORT],
                CONF_TOKEN: config[CONF_TOKEN],
            }
        )
        self._addon_slug = discovery_info.slug
        self._discovery = dict(config)
        self.context["title_placeholders"] = {"addon": self._engine.addon_name}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm setup of the discovered add-on."""
        if user_input is not None:
            return await self.async_step_on_supervisor({CONF_USE_ADDON: True})
        # The hassio_confirm title/description use both {engine} and {addon};
        # title_placeholders only fills the flow title, so the step form needs
        # both placeholders itself.
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={
                "engine": self._engine.name,
                "addon": self._engine.addon_name,
            },
        )

    async def async_step_on_supervisor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Branch on the (already-resolved) add-on's install/run state."""
        if user_input is None:
            return self.async_show_form(
                step_id="on_supervisor",
                data_schema=ON_SUPERVISOR_SCHEMA,
                description_placeholders={
                    "engine": self._engine.name,
                    "addon": self._engine.addon_name,
                },
            )
        if not user_input[CONF_USE_ADDON]:
            return self.async_abort(reason="addon_required")

        try:
            info = await self._addon_manager.async_get_addon_info()
        except AddonError as err:
            LOGGER.error(
                "Failed to get %s add-on info: %s", self._engine.addon_name, err
            )
            raise AbortFlow("addon_info_failed") from err

        if info.state is AddonState.RUNNING:
            return await self.async_step_finish()
        if info.state is AddonState.NOT_RUNNING:
            return await self.async_step_start_addon()
        return await self.async_step_install_addon()

    async def async_step_pick_addon(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose when more than one add-on could be meant."""
        if user_input is not None:
            self._addon_slug = user_input[CONF_ADDON]
            return await self.async_step_on_supervisor()
        return self.async_show_form(
            step_id="pick_addon",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDON): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=self._addon_choices)
                    )
                }
            ),
            description_placeholders={"addon": self._engine.addon_name},
        )

    async def async_step_install_addon(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Install the add-on, showing progress."""
        if self.install_task is None:
            self.install_task = self.hass.async_create_task(self._async_install_addon())
        if not self.install_task.done():
            return self.async_show_progress(
                step_id="install_addon",
                progress_action="install_addon",
                progress_task=self.install_task,
                description_placeholders={"addon": self._engine.addon_name},
            )
        try:
            await self.install_task
        except AddonError as err:
            LOGGER.error(
                "Failed to install %s add-on: %s", self._engine.addon_name, err
            )
            return self.async_show_progress_done(next_step_id="install_failed")
        finally:
            self.install_task = None
        return self.async_show_progress_done(next_step_id="start_addon")

    async def async_step_install_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Abort after a failed install."""
        return self.async_abort(reason="addon_install_failed")

    async def async_step_start_addon(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the add-on, showing progress."""
        if self.start_task is None:
            self.start_task = self.hass.async_create_task(self._async_start_addon())
        if not self.start_task.done():
            return self.async_show_progress(
                step_id="start_addon",
                progress_action="start_addon",
                progress_task=self.start_task,
                description_placeholders={"addon": self._engine.addon_name},
            )
        try:
            await self.start_task
        except AddonError as err:
            LOGGER.error("Failed to start %s add-on: %s", self._engine.addon_name, err)
            return self.async_show_progress_done(next_step_id="start_failed")
        finally:
            self.start_task = None
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_start_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Abort after a failed start."""
        return self.async_abort(reason="addon_start_failed")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read the add-on's connection details, test them, and create the entry."""
        if self._discovery is None:
            self._discovery = await self._async_addon_connection_info()

        assert self._addon_slug is not None
        host = self._discovery[CONF_HOST]
        port = self._discovery[CONF_PORT]
        token = self._discovery[CONF_TOKEN]

        await self.async_set_unique_id(self._addon_slug, raise_on_progress=False)
        self._abort_if_unique_id_configured()

        engine = self._engine
        client = ClaudeClient(
            async_get_clientsession(self.hass),
            base_url=f"http://{host}:{port}",
            token=token,
            engine=engine,
        )
        try:
            await client.async_get_status()
        except ClaudeEngineMismatchError as err:
            LOGGER.error(
                "The %s add-on reports engine %r, not %r",
                engine.addon_name,
                err.reported,
                engine.key,
            )
            return self.async_abort(reason="engine_mismatch")
        except ClaudeError as err:
            LOGGER.error("Could not reach the %s add-on: %s", engine.addon_name, err)
            return self.async_abort(reason="cannot_connect")

        return self.async_create_entry(
            title=engine.addon_name,
            data={
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_TOKEN: token,
                CONF_ADDON_SLUG: self._addon_slug,
                CONF_ENGINE: engine.key,
            },
        )

    @property
    def _engine(self) -> Engine:
        """The engine of the add-on this flow sets up.

        Resolved from the add-on slug once it is known; before that (only
        while ``add_repository`` is showing its form, ahead of a slug) it is
        the engine chosen in the ``engine`` step.
        """
        if self._addon_slug is not None:
            engine = engine_for_slug(self._addon_slug)
            assert engine is not None
            return engine
        assert self._engine_key is not None
        return ENGINES[self._engine_key]

    @property
    def _addon_manager(self) -> AddonManager:
        assert self._addon_slug is not None
        return get_addon_manager(self.hass, self._addon_slug)

    async def _async_install_addon(self) -> None:
        await self._addon_manager.async_schedule_install_addon()

    async def _async_start_addon(self) -> None:
        await self._addon_manager.async_schedule_start_addon()

    async def _async_addon_connection_info(self) -> dict[str, Any]:
        """Obtain host/port/token from add-on discovery, falling back to options."""
        addon = self._addon_manager
        try:
            discovery = await addon.async_get_addon_discovery_info()
        except AddonError:
            discovery = None

        if discovery and all(
            k in discovery for k in (CONF_HOST, CONF_PORT, CONF_TOKEN)
        ):
            return dict(discovery)

        # Fallback (contract §1): read the token from add-on options + derive
        # host/port.
        try:
            info: AddonInfo = await addon.async_get_addon_info()
        except AddonError as err:
            raise AbortFlow("addon_get_discovery_info_failed") from err

        token = info.options.get(ADDON_OPTION_API_TOKEN)
        if not token or not info.hostname:
            raise AbortFlow("addon_get_discovery_info_failed")
        return {
            CONF_HOST: info.hostname,
            CONF_PORT: info.options.get("api_port", DEFAULT_PORT),
            CONF_TOKEN: token,
        }


class ClaudeOptionsFlow(OptionsFlow):
    """Options for how chat-driven actions are confirmed."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the auto-execute and critical-entities options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        engine = engine_for_entry(self.config_entry)
        assert engine is not None
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_AUTO_EXECUTE,
                    default=options.get(CONF_AUTO_EXECUTE, True),
                ): bool,
                vol.Optional(
                    CONF_CRITICAL_ENTITIES,
                    default=options.get(CONF_CRITICAL_ENTITIES, []),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True)
                ),
                vol.Required(
                    CONF_CAMERA_VISION,
                    default=options.get(CONF_CAMERA_VISION, False),
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={"engine": engine.name},
        )

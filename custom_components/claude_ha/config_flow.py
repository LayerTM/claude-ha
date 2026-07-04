"""Config flow for the Claude for Home Assistant integration."""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.components.hassio import (
    AddonError,
    AddonInfo,
    AddonManager,
    AddonState,
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

from .addon import async_resolve_addon_slug, get_addon_manager
from .api import ClaudeClient, ClaudeError
from .const import (
    ADDON_NAME,
    ADDON_OPTION_API_TOKEN,
    ADDON_SLUG_SUFFIX,
    CONF_ADDON_SLUG,
    CONF_AUTO_EXECUTE,
    CONF_CRITICAL_ENTITIES,
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    CONF_USE_ADDON,
    DEFAULT_PORT,
    DOMAIN,
    LOGGER,
)

ON_SUPERVISOR_SCHEMA = vol.Schema({vol.Required(CONF_USE_ADDON, default=True): bool})


class ClaudeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Claude, backed by the Claude Code add-on."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ClaudeOptionsFlow:
        """Return the options flow."""
        return ClaudeOptionsFlow()

    def __init__(self) -> None:
        """Init flow state."""
        self._addon_slug: str | None = None
        self._discovery: dict[str, Any] | None = None
        self.install_task: asyncio.Task[None] | None = None
        self.start_task: asyncio.Task[None] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a user-initiated flow. Requires Supervisor + the add-on."""
        if not is_hassio(self.hass):
            return self.async_abort(reason="not_hassio")
        return await self.async_step_on_supervisor()

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Handle add-on discovery (the add-on advertised host/port/token)."""
        if not discovery_info.slug.endswith(ADDON_SLUG_SUFFIX):
            return self.async_abort(reason="not_claude_addon")

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
        self.context["title_placeholders"] = {"addon": ADDON_NAME}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm setup of the discovered add-on."""
        if user_input is not None:
            return await self.async_step_on_supervisor({CONF_USE_ADDON: True})
        # The hassio_confirm description uses {addon}; title_placeholders only
        # fills the flow title, so the step description needs its own placeholder.
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={"addon": ADDON_NAME},
        )

    async def async_step_on_supervisor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Resolve the add-on and branch on its install/run state."""
        if self._addon_slug is None:
            self._addon_slug = await async_resolve_addon_slug(self.hass)
            if self._addon_slug is None:
                return self.async_abort(reason="addon_not_found")

        if user_input is None:
            return self.async_show_form(
                step_id="on_supervisor", data_schema=ON_SUPERVISOR_SCHEMA
            )
        if not user_input[CONF_USE_ADDON]:
            return self.async_abort(reason="addon_required")

        try:
            info = await self._addon_manager.async_get_addon_info()
        except AddonError as err:
            LOGGER.error("Failed to get Claude Code add-on info: %s", err)
            raise AbortFlow("addon_info_failed") from err

        if info.state is AddonState.RUNNING:
            return await self.async_step_finish()
        if info.state is AddonState.NOT_RUNNING:
            return await self.async_step_start_addon()
        return await self.async_step_install_addon()

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
            )
        try:
            await self.install_task
        except AddonError as err:
            LOGGER.error("Failed to install Claude Code add-on: %s", err)
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
            )
        try:
            await self.start_task
        except AddonError as err:
            LOGGER.error("Failed to start Claude Code add-on: %s", err)
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

        client = ClaudeClient(
            async_get_clientsession(self.hass),
            base_url=f"http://{host}:{port}",
            token=token,
        )
        try:
            await client.async_get_status()
        except ClaudeError as err:
            LOGGER.error("Could not reach the Claude Code add-on: %s", err)
            return self.async_abort(reason="cannot_connect")

        return self.async_create_entry(
            title=ADDON_NAME,
            data={
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_TOKEN: token,
                CONF_ADDON_SLUG: self._addon_slug,
            },
        )

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
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

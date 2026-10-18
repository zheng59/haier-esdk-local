"""配置流程：填空调的局域网 IP → 现场连一次验证 → 建条目。"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback

from .client import HaierError
from .const import (CONF_DEVICE_ID, CONF_HOST, CONF_PORT, CONF_REDISCOVER,
                    CONF_SCAN_INTERVAL, DEFAULT_PORT, DEFAULT_REDISCOVER,
                    DEFAULT_SCAN_INTERVAL, DOMAIN)
from .coordinator import HaierCoordinator

_LOGGER = logging.getLogger(__name__)


def _schema(host: str = "", port: int = DEFAULT_PORT,
            interval: int = DEFAULT_SCAN_INTERVAL,
            rediscover: bool = DEFAULT_REDISCOVER) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_HOST, default=host): str,
        vol.Optional(CONF_PORT, default=port): vol.All(int, vol.Range(min=1, max=65535)),
        vol.Optional(CONF_SCAN_INTERVAL, default=interval):
            vol.All(int, vol.Range(min=10, max=900)),
        vol.Optional(CONF_REDISCOVER, default=rediscover): bool,
    })


class HaierEsdkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """一台空调一个条目。"""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            try:
                info = await HaierCoordinator.async_probe(self.hass, host, port)
            except HaierError as exc:
                _LOGGER.debug("探测失败（协议）：%s", exc)
                errors["base"] = "cannot_connect"
            except (OSError, TimeoutError) as exc:
                _LOGGER.debug("探测失败（网络）：%s", exc)
                errors["base"] = "cannot_connect"
            else:
                device_id = info.get("device_id") or f"host-{host}"
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: host, CONF_PORT: port})
                suffix = device_id[-6:] if len(device_id) >= 6 else host
                return self.async_create_entry(
                    title=f"海尔空调 {suffix}",
                    data={CONF_HOST: host,
                          CONF_PORT: port,
                          CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                          CONF_REDISCOVER: bool(user_input.get(CONF_REDISCOVER,
                                                               DEFAULT_REDISCOVER)),
                          CONF_DEVICE_ID: device_id},
                )
        return self.async_show_form(step_id="user", data_schema=_schema(), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        flow = HaierEsdkOptionsFlow()
        # HA ≥2025.1 由框架注入 self.config_entry；老版本用它兜底（不主动赋 config_entry，
        # 否则新版 HA 会报弃用告警）
        flow._legacy_entry = config_entry
        return flow


class HaierEsdkOptionsFlow(config_entries.OptionsFlow):
    """改 IP / 端口 / 轮询间隔（模块同时只接受一个连接，别调太快）。

    改 IP 或端口时会**现场连一次验证**，连不上就不让保存 —— 免得手滑把条目改废。
    """

    _legacy_entry: config_entries.ConfigEntry | None = None

    async def async_step_init(self, user_input: dict | None = None):
        entry = getattr(self, "config_entry", None) or self._legacy_entry
        current_host = entry.options.get(CONF_HOST, entry.data.get(CONF_HOST, ""))
        current_port = int(entry.options.get(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT)))
        current_interval = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        current_rediscover = bool(entry.options.get(
            CONF_REDISCOVER, entry.data.get(CONF_REDISCOVER, DEFAULT_REDISCOVER)))

        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            port = int(user_input[CONF_PORT])
            if (host, port) != (current_host, current_port):
                try:
                    await HaierCoordinator.async_probe(self.hass, host, port)
                except (HaierError, OSError, TimeoutError) as exc:
                    _LOGGER.debug("选项里改连接参数后探测失败：%s", exc)
                    errors["base"] = "cannot_connect"
            if not errors:
                return self.async_create_entry(title="", data={
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_REDISCOVER: bool(user_input.get(CONF_REDISCOVER,
                                                         current_rediscover)),
                })
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(current_host, current_port, int(current_interval),
                                current_rediscover),
            errors=errors,
        )

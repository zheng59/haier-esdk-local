"""海尔空调（eSDK 模块本地直连）。

链路：Home Assistant → TCP <空调 IP>:56800（明文 EPP，无需账号/密钥/云）。
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import HaierCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = HaierCoordinator(hass, entry)
    # 首次连不上就直接失败，让配置流程能提示用户（而不是建出一个永远不可用的条目）
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("海尔空调已接入：%s（设备 %s）", coordinator.host, coordinator.device_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """改配置（如轮询间隔）后重载。

    例外：自动重发现只改了 IP，协调器已经切到新 IP 了 —— 这时重载纯属浪费，
    还会打断正在跑的轮询，所以直接跳过。
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and coordinator.matches_entry(entry):
        return
    await hass.config_entries.async_reload(entry.entry_id)

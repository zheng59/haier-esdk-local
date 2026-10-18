"""轮询协调器：把同步的 TCP 客户端放进执行器里跑，并串行化所有访问。

模块**同时只接受一个 TCP 连接**（对高频连接还会短暂限流），所以：
* 所有请求都过同一把 asyncio 锁；
* 每次操作开一个短连接，用完立刻关；
* 读状态时顺手把模块连接时主动推来的故障位图留下来（省一次往返）。

另外负责「IP 变了自动找回」：轮询连不上时扫一遍本地网段（见 discovery.py），
用设备 ID（MAC）认人，认到了就把条目里的 host 改掉，下一次轮询就能连上。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from time import monotonic
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import discovery
from .client import DEFAULT_MAC, HaierClient, HaierCommandRejected, HaierError
from .const import (CONF_DEVICE_ID, CONF_HOST, CONF_MAC, CONF_PORT, CONF_REDISCOVER,
                    CONF_SCAN_INTERVAL, DEFAULT_PORT, DEFAULT_REDISCOVER,
                    DEFAULT_SCAN_INTERVAL, DOMAIN)
from .protocol import TYPE_ALARM, HaierStatus, alarm_bits

_LOGGER = logging.getLogger(__name__)

Job = Callable[[HaierClient], HaierStatus]

# 两次「找 IP」之间至少隔这么久（秒）：空调关机 / 真不在网里的时候别一直扫网段
DISCOVERY_COOLDOWN = 300.0            # 之前连通过（大概率就是 IP 变了）→ 5 分钟扫一次
NEVER_CONNECTED_COOLDOWN = 900.0      # 本次启动从没连上过（关机 / 还没配好）→ 15 分钟扫一次
# entry_id → 上次扫描时间。放模块级：条目重试会重建协调器，冷却不能跟着重置
_LAST_SCAN: dict[str, float] = {}

# 连不上 / 断了 / 超时都算「链路问题」，值得去找 IP；INVALID（命令被拒）不算
NetworkError = (HaierError, OSError, TimeoutError)


def _is_network_error(exc: BaseException) -> bool:
    return not isinstance(exc, HaierCommandRejected)


class HaierCoordinator(DataUpdateCoordinator[HaierStatus]):
    """一辆空调 = 一个协调器。"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        # 选项优先（用户可以在「配置 → 选项」里改 IP / 端口 / 轮询间隔）
        host: str = entry.options.get(CONF_HOST, entry.data[CONF_HOST])
        port: int = int(entry.options.get(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT)))
        interval: int = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(hass, _LOGGER,
                         name=f"{DOMAIN}-{host}",
                         update_interval=timedelta(seconds=interval))
        self.entry = entry
        self.host, self.port = host, port
        self.mac: bytes = (entry.data.get(CONF_MAC) or DEFAULT_MAC)
        if isinstance(self.mac, str):
            self.mac = self.mac.encode("ascii")
        self.device_id: str | None = entry.data.get(CONF_DEVICE_ID)
        self.rediscover: bool = bool(entry.options.get(
            CONF_REDISCOVER, entry.data.get(CONF_REDISCOVER, DEFAULT_REDISCOVER)))
        self.last_fault: bytes = b""
        self._lock = asyncio.Lock()
        self._ever_ok = False

    # ------------------------------------------------------------------ 读
    async def _async_update_data(self) -> HaierStatus:
        try:
            return await self._read()
        except NetworkError as exc:
            # 连不上：可能只是空调换了 IP —— 找回来再读一次
            if not _is_network_error(exc) or not await self._async_rediscover():
                raise UpdateFailed(str(exc)) from exc
        try:
            return await self._read()
        except NetworkError as exc:
            raise UpdateFailed(str(exc)) from exc

    async def _read(self) -> HaierStatus:
        status = await self._run(lambda client: client.read_status())
        self._ever_ok = True
        return status

    # ------------------------------------------------------------------ 写
    async def async_write(self, job: Job) -> HaierStatus:
        """写操作：异常向上抛给实体（不吞成 UpdateFailed）。

        如果是「连不上」（IP 刚变 / 断了一下），先找一次 IP 再重试一遍 ——
        用户点的开关不该白点。
        """
        try:
            status = await self._run(job)
        except NetworkError as exc:
            if not _is_network_error(exc) or not await self._async_rediscover():
                raise
            status = await self._run(job)
        self.async_set_updated_data(status)
        return status

    # ------------------------------------------------------------------ 底层
    async def _run(self, job: Job) -> HaierStatus:
        async with self._lock:
            return await self.hass.async_add_executor_job(self._run_sync, job)

    def _run_sync(self, job: Job) -> HaierStatus:
        with HaierClient(self.host, self.port, self.mac) as client:
            status = job(client)
            self._remember_fault(client)
            if client.device_id and not self.device_id:
                self.device_id = client.device_id
            return status

    def _remember_fault(self, client: HaierClient) -> None:
        """连接时模块会主动推故障位图（type=0x04），白拿的，不用额外请求。

        位图含义厂家没公开 → 只记「置位号」；有变化才写日志（故障出现/清除都留痕）。
        """
        for env in client.burst:
            if env.frame.frame_type != TYPE_ALARM or len(env.frame.data) < 2:
                continue
            fault = bytes(env.frame.data[2:])
            if fault != self.last_fault:
                if any(fault):
                    _LOGGER.warning("空调报故障：位图 %s（置位 %s）", fault.hex(), alarm_bits(fault))
                elif self.last_fault:
                    _LOGGER.info("空调故障已清除（原位图 %s）", self.last_fault.hex())
            self.last_fault = fault

    # ------------------------------------------------------------------ IP 变了自动找回
    async def _async_rediscover(self) -> bool:
        """扫本地网段，用设备 ID（MAC）把同一台空调找回来，并改掉条目里的 IP。

        返回 True 表示「IP 已更新，可以重试一次」。任何一步失败都只当没找到 ——
        轮询该失败还是失败，不能因为扫描把轮询搞挂。
        """
        if not self.rediscover:
            return False
        now = monotonic()
        cooldown = DISCOVERY_COOLDOWN if self._ever_ok else NEVER_CONNECTED_COOLDOWN
        last = _LAST_SCAN.get(self.entry.entry_id)
        if last is not None and now - last < cooldown:
            return False
        _LAST_SCAN[self.entry.entry_id] = now       # 先占坑：条目重试时不会连环扫

        _LOGGER.warning("连不上 %s，开始在本地网段里找设备 %s …（%s）", self.host,
                        self.device_id or "（设备 ID 未知）",
                        "之前是通的" if self._ever_ok else "本次启动还没连上过")
        try:
            found = await discovery.async_find_device(
                self.hass, self.device_id, self.port, hint_host=self.host)
        except Exception as exc:                    # noqa: BLE001 扫描出错不许影响轮询
            _LOGGER.debug("扫网段失败：%s", exc)
            return False

        if found is None:
            _LOGGER.warning("没找到这台空调（关机了 / 换了网段 / 不在同一个子网？），"
                            "%d 秒后再试", int(cooldown))
            return False
        if found == self.host:
            _LOGGER.warning("设备 %s 还在 %s：连不上的原因不是 IP 变了（端口被占 / 网络不通？）",
                            self.device_id, found)
            return False

        old, self.host = self.host, found
        self._update_entry_host(old, found)
        return True

    def _update_entry_host(self, old: str, new: str) -> None:
        """把新 IP 写回条目选项（写回后 host 会一直是这个值，重启也不丢）。"""
        options = dict(self.entry.options)
        options[CONF_HOST] = new
        options.setdefault(CONF_PORT, self.port)
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        _LOGGER.warning("空调 IP 变了：%s → %s，已自动更新到集成配置里（不用手工改）", old, new)

    def matches_entry(self, entry: ConfigEntry) -> bool:
        """条目的连接参数和本协调器现在用的一致吗？

        自动重发现改完 IP 后 HA 会通知更新监听器；这时参数其实已经生效了，
        不需要（也不应该）重载条目 —— 靠这个方法识别出来。
        """
        host = entry.options.get(CONF_HOST, entry.data.get(CONF_HOST))
        port = int(entry.options.get(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT)))
        interval = int(entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)))
        return (host == self.host and port == self.port
                and interval == int(self.update_interval.total_seconds()))

    # ------------------------------------------------------------------ 供配置流程复用
    @staticmethod
    async def async_probe(hass: HomeAssistant, host: str,
                          port: int = DEFAULT_PORT) -> dict[str, Any]:
        """连一次、读一次，返回设备标识（配置流程用来验活 + 取 unique_id）。"""

        def _probe() -> dict[str, Any]:
            with HaierClient(host, port) as client:
                status = client.read_status()
                return {"device_id": client.device_id, "status": status}

        return await hass.async_add_executor_job(_probe)

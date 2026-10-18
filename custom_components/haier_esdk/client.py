"""海尔 eSDK 模块本地客户端（同步、纯标准库）。

设计取舍：
* 模块**同时只接受一个 TCP 连接**，且对高频连接有限流 → 所有操作都串行、每次用完就关。
* 一次「会话」= connect（顺便收下模块主动推的状态）→ 一条或多条请求 → close。
* 写操作一律「先读回当前 24 字节控制块 → 改要改的字段 → 4d5f 回填」，
  和 ESPHome 的 smartair2 做法一致；开关机走专用的 4d02/4d03。
* 空调拒绝的命令会回 `type=0x03 INVALID`，这里转成 HaierError 抛出来。

实测结论（2026-10-17，KFR-35G/BAA21AU1）：
    可用：开关 / 模式 / 设定温度(整度 16~30) / 风速 / 健康 / 摆风 / 遥控锁
    被忽略：显示屏开关 / 极速 turbo / 静音 quiet / 半度(0.5℃)  ← 半度会被回 INVALID
"""
from __future__ import annotations

import socket
import time
from typing import Callable

from .protocol import (ADDR_AC, ADDR_MODULE, CMD_GET_STATUS, CMD_POWER_OFF, CMD_POWER_ON,
                       CMD_SET, FANS, MODES, TYPE_ALARM_RESPONSE, TYPE_GET_ALARM,
                       TYPE_INVALID, TYPE_REPORT, TYPE_STATUS, Envelope, HaierStatus,
                       build_request, epp_frame, parse_envelopes)

DEFAULT_PORT = 56800
DEFAULT_MAC = b"001122334455"
CMD_GET_STATUS_WORD = CMD_GET_STATUS.to_bytes(2, "big")


class HaierError(Exception):
    """通信或协议错误。"""


class HaierCommandRejected(HaierError):
    """空调明确回了 INVALID（命令被拒/参数不支持）。"""


def _mac_bytes(mac: bytes | str) -> bytes:
    if isinstance(mac, str):
        return mac.encode("ascii")[:12].ljust(12, b"\x00")
    return bytes(mac)[:12].ljust(12, b"\x00")


class HaierClient:
    """一次会话。推荐用 with。"""

    def __init__(self, host: str, port: int = DEFAULT_PORT, mac: bytes | str = DEFAULT_MAC,
                 timeout: float = 5.0, burst_wait: float = 2.0) -> None:
        self.host, self.port = host, port
        self.mac = _mac_bytes(mac)
        self.timeout, self.burst_wait = timeout, burst_wait
        self._sock: socket.socket | None = None
        self._buf = b""
        self._seq = 0
        self.burst: list[Envelope] = []
        self.device_id: str | None = None

    # ------------------------------------------------------------------ 连接
    def __enter__(self) -> "HaierClient":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> "HaierClient":
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(0.3)
            self._buf, self._seq, self.burst = b"", 0, []
            # 连上后模块会主动推 2 条（故障位图 + 状态）；状态那条可以顺手当第一次读数。
            deadline = time.monotonic() + self.burst_wait
            while time.monotonic() < deadline:
                got = self._drain(deadline)
                self.burst.extend(got)
                if any(self._is_status(e) for e in got) or self.burst:
                    break
            for env in self.burst:
                if self.device_id is None and len(env.raw_envelope) >= 52:
                    self.device_id = env.raw_envelope[40:52].decode("ascii", "replace").strip("\x00")
        except BaseException:
            self.close()                     # 连到一半失败也别漏 socket（扫网段时会连很多台）
            raise
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ------------------------------------------------------------------ 收发
    @staticmethod
    def _is_status(env: Envelope) -> bool:
        return env.frame.frame_type in (TYPE_STATUS, TYPE_REPORT) and env.frame.subcommand == 0x6D01

    def _drain(self, deadline: float) -> list[Envelope]:
        out: list[Envelope] = []
        while time.monotonic() < deadline:
            try:
                chunk = self._sock.recv(4096)            # type: ignore[union-attr]
            except socket.timeout:
                break
            except OSError as exc:
                raise HaierError(f"读取失败：{exc}") from exc
            if not chunk:
                raise HaierError("对端关闭了连接")
            self._buf += chunk
        parsed, self._buf = parse_envelopes(self._buf)
        out.extend(parsed)
        return out

    def request(self, frame: bytes,
                want: tuple[int, ...] = (TYPE_STATUS, TYPE_REPORT),
                wait: float = 4.0, retries: int = 1) -> Envelope:
        """发一条 EPP 帧并等应答（序号匹配 + 类型符合）。"""
        if self._sock is None:
            raise HaierError("还没 connect()")
        for attempt in range(retries + 1):
            self._seq = (self._seq + 1) & 0xFF
            seq = self._seq
            try:
                self._sock.sendall(build_request(frame, seq=seq, mac=self.mac))
            except OSError as exc:
                raise HaierError(f"发送失败：{exc}") from exc
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                for env in self._drain(deadline):
                    if env.seq != seq:
                        continue
                    if env.frame.frame_type == TYPE_INVALID:
                        raise HaierCommandRejected(
                            f"空调拒绝了该命令（INVALID，data={env.frame.data.hex()}）")
                    if env.frame.frame_type not in want:
                        continue
                    if env.frame.frame_type in (TYPE_STATUS, TYPE_REPORT) and not self._is_status(env):
                        continue
                    return env
            if attempt < retries:
                time.sleep(0.4)
        raise HaierError(f"等待应答超时（试了 {retries + 1} 次）")

    # ------------------------------------------------------------------ 读
    def read_status(self) -> HaierStatus:
        """4d01：读完整状态。"""
        return HaierStatus.from_frame(self.request(epp_frame(CMD_GET_STATUS_WORD, addr=ADDR_AC)).frame)

    def status_from_burst(self) -> HaierStatus | None:
        """直接用连接时模块推的那条状态（省一次往返）。"""
        for env in self.burst:
            if self._is_status(env):
                return HaierStatus.from_frame(env.frame)
        return None

    def read_fault(self) -> bytes:
        """0x73：读故障位图（全 0 = 无故障）。"""
        frame = epp_frame(b"", frame_type=TYPE_GET_ALARM, addr=ADDR_AC)
        data = self.request(frame, want=(TYPE_ALARM_RESPONSE,)).frame.data
        return data[2:] if len(data) >= 2 else data

    # ------------------------------------------------------------------ 写
    def set_power(self, on: bool) -> HaierStatus:
        """4d02/4d03：开机 / 关机。"""
        cmd = CMD_POWER_ON if on else CMD_POWER_OFF
        return HaierStatus.from_frame(self.request(epp_frame(cmd.to_bytes(2, "big"),
                                                             addr=ADDR_AC)).frame)

    def apply(self, status: HaierStatus) -> HaierStatus:
        """4d5f：把 24 字节控制块整体回填。"""
        payload = CMD_SET.to_bytes(2, "big") + status.for_sending()
        return HaierStatus.from_frame(self.request(epp_frame(payload, addr=ADDR_AC)).frame)

    def set_bits(self, index: int, mask: int, on: bool) -> HaierStatus:
        """按位改控制块（用于 set_hvac 没覆盖的开关位）。"""
        current = self.status_from_burst() or self.read_status()
        return self.apply(current.with_bits(index, mask, on))

    def set_hvac(self, *, power: bool | None = None, mode: str | None = None,
                 fan: str | None = None, temperature: float | None = None,
                 health: bool | None = None, swing: bool | None = None) -> HaierStatus:
        """一次会话里「读 → 改 → 写」，只动指明的字段。

        开关机走专用命令；其余字段合成一条 4d5f（和 ESPHome 一样的做法）。

        当前状态优先用「连接时模块主动推的那条」（白拿的，省一次往返）；
        没收到推送才补一条 4d01。
        """
        current = self.status_from_burst() or self.read_status()
        handled_power = False
        if power is not None and power != current.power:
            current = self.set_power(power)
            handled_power = True
        target = current
        if temperature is not None:
            target = target.with_target(temperature)
        if mode:
            target = target.with_mode(mode)
        if fan:
            target = target.with_fan(fan)
        if health is not None:
            target = target.with_health(health)
        if swing is not None:
            target = target.with_swing(swing)
        if target.raw == current.raw:
            return current
        return self.apply(target)

    # ------------------------------------------------------------------ 便捷
    def roundtrip(self, job: Callable[["HaierClient"], HaierStatus]) -> HaierStatus:
        """在同一个会话里跑一段自定义逻辑。"""
        return job(self)


def format_status(status: HaierStatus) -> str:
    """给日志/调试用的一行中文摘要。"""
    d = status.as_dict()
    return (f"开关={'开' if d['power'] else '关'}  模式={d['mode']}  设定={d['target_temperature']:.0f}℃  "
            f"风速={d['fan']}  室温={d['room_temperature']:.0f}℃  湿度={d['room_humidity']}%  "
            f"健康={'开' if d['health'] else '关'}  摆风={d['swing']}")


__all__ = ["HaierClient", "HaierError", "HaierCommandRejected", "format_status",
           "DEFAULT_PORT", "DEFAULT_MAC", "MODES", "FANS", "ADDR_AC", "ADDR_MODULE"]

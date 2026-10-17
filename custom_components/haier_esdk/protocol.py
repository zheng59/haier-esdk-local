#!/usr/bin/env python3
"""海尔 eSDK 模块（KZW 一代）本地协议：信封 + EPP 帧 + smartair2 状态块。

这是**唯一实现**（custom_components/haier_esdk/protocol.py），仓库根目录的
haier_protocol.py 只是转发用的兼容层。

链路：TCP <空调IP>:56800，明文，无需密钥/账号。

信封（80 字节头 + EPP 帧）::

    00 00 27 14 | 0×36 | MAC(12 ASCII) | 0×4 | 0×16 | 序号(BE32) | 帧长(BE32) | EPP 帧
    ^^^^^^^^^^^ 请求方向用 2714；模块→客户端用 2715，其余布局相同（序号回传）

EPP 帧::

    ff ff | L | addr(6) | type | data | checksum
    L = 6 + 1 + len(data) + 1
    checksum = (L + addr + type + data) & 0xFF   （累加和，不是补码）

smartair2 状态块（type=0x02/0x06 的 data 前 2 字节为命令字，后面 24 字节为控制块）::

    c0  温度高字节   c1  室温(1℃)      c2  湿度高字节   c3  湿度(%)
    c5  cntrl        c11 模式           c13 风速         c15 摆风
    c16 bit3=华氏  bit7=锁定遥控
    c17 bit0=开关  bit3=健康  bit5=半度  bit6=十度
    c19 bit0=摆风位  bit1=极速 bit2=静音 bit3=水平摆风 bit4=垂直摆风 bit5=显示关
    c23 设定温度（+16℃）

本机（KFR-35G/BAA21AU1，2026-10-17 实测）支持：开关 / 模式 / 整度温度 / 风速 / 摆风(c15) /
健康 / 遥控锁 / c19 bit0；**不支持**（主板直接丢弃）：显示屏开关、极速、静音、半度、
华氏、十度、水平/垂直摆风细分。
"""
from __future__ import annotations

import dataclasses
import struct

MAGIC_REQUEST = b"\x00\x00\x27\x14"
MAGIC_RESPONSE = b"\x00\x00\x27\x15"
HEADER_LEN = 80

TYPE_CONTROL = 0x01
TYPE_STATUS = 0x02
TYPE_INVALID = 0x03
TYPE_ALARM = 0x04
TYPE_CONFIRM = 0x05
TYPE_REPORT = 0x06
TYPE_GET_VERSION = 0x61
TYPE_GET_ALARM = 0x73
TYPE_ALARM_RESPONSE = 0x74

ADDR_AC = b"\x00\x00\x00\x00\x00\x00"
ADDR_MODULE = b"\x00\x00\x00\x00\x00\x01"

CMD_GET_STATUS = 0x4D01
CMD_POWER_ON = 0x4D02
CMD_POWER_OFF = 0x4D03
CMD_SET = 0x4D5F

MODES = {0x00: "auto", 0x01: "cool", 0x02: "heat", 0x03: "fan", 0x04: "dry"}
MODES_INV = {v: k for k, v in MODES.items()}
FANS = {0x00: "high", 0x01: "mid", 0x02: "low", 0x03: "auto"}
FANS_INV = {v: k for k, v in FANS.items()}
PRESETS = {0: "none", 1: "turbo", 2: "quiet", 3: "away"}
FRAME_NAMES = {0x01: "CONTROL", 0x02: "STATUS", 0x03: "INVALID", 0x04: "ALARM",
               0x05: "CONFIRM", 0x06: "REPORT", 0x61: "GET_VERSION",
               0x62: "VERSION_RESP", 0x73: "GET_ALARM", 0x74: "ALARM_RESP"}


# --------------------------------------------------------------------------- EPP
def epp_frame(data: bytes, frame_type: int = TYPE_CONTROL,
              addr: bytes = ADDR_AC) -> bytes:
    """组一条 EPP 帧（含累加校验和）。"""
    if len(addr) != 6:
        raise ValueError("地址必须是 6 字节")
    length = 6 + 1 + len(data) + 1
    body = addr + bytes([frame_type]) + data
    return b"\xff\xff" + bytes([length]) + body + bytes([(length + sum(body)) & 0xFF])


@dataclasses.dataclass
class EppFrame:
    addr: bytes
    frame_type: int
    data: bytes
    checksum_ok: bool
    raw: bytes

    @property
    def subcommand(self) -> int:
        return int.from_bytes(self.data[:2], "big") if len(self.data) >= 2 else 0


def parse_epp(frame: bytes) -> EppFrame | None:
    if len(frame) < 4 or frame[:2] != b"\xff\xff":
        return None
    length = frame[2]
    if len(frame) < 3 + length:
        return None
    body = frame[3:3 + length]
    return EppFrame(addr=body[:6], frame_type=body[6], data=body[7:-1],
                    checksum_ok=((sum(frame[2:2 + length]) & 0xFF) == body[-1]),
                    raw=frame[:3 + length])


# ----------------------------------------------------------------------- 信封
def build_request(frame: bytes, seq: int, mac: bytes = b"001122334455") -> bytes:
    head = (MAGIC_REQUEST + b"\x00" * 36 + mac.ljust(12, b"\x00")[:12]
            + b"\x00" * 20 + (seq & 0xFFFFFFFF).to_bytes(4, "big")
            + len(frame).to_bytes(4, "big"))
    assert len(head) == HEADER_LEN, len(head)
    return head + frame


@dataclasses.dataclass
class Envelope:
    magic: bytes
    seq: int
    frame: EppFrame
    raw_envelope: bytes = b""          # 完整封装（80 字节头 + 帧），调试与取设备 ID 用


def parse_envelopes(buf: bytes) -> tuple[list[Envelope], bytes]:
    """从字节流里切出完整信封，返回 (已解析, 剩余未完整字节)。"""
    out: list[Envelope] = []
    off = 0
    while True:
        # 注意 off+4 的边界：缓冲区末尾只剩半个魔数时必须留着，不能当垃圾跳过
        while off + 4 <= len(buf) and buf[off:off + 4] not in (MAGIC_REQUEST, MAGIC_RESPONSE):
            off += 1
        if off + HEADER_LEN > len(buf):
            break
        length = int.from_bytes(buf[off + 76:off + 80], "big")
        if length <= 0 or length > 4096 or off + HEADER_LEN + length > len(buf):
            off += 1
            continue
        frame = parse_epp(buf[off + HEADER_LEN:off + HEADER_LEN + length])
        if frame is not None:
            out.append(Envelope(magic=buf[off:off + 4],
                                seq=int.from_bytes(buf[off + 72:off + 76], "big"),
                                frame=frame,
                                raw_envelope=buf[off:off + HEADER_LEN + length]))
        off += HEADER_LEN + length
    return out, buf[off:]


# ------------------------------------------------------------------ 状态块
@dataclasses.dataclass
class HaierStatus:
    """smartair2 的 24 字节控制块。"""
    raw: bytes

    def __post_init__(self) -> None:
        if len(self.raw) != 24:
            raise ValueError(f"控制块必须 24 字节，收到 {len(self.raw)}")

    @classmethod
    def from_frame(cls, frame: EppFrame) -> "HaierStatus":
        if len(frame.data) < 26:
            raise ValueError(f"状态帧太短：{len(frame.data)}")
        return cls(bytes(frame.data[2:26]))

    # 读
    @property
    def power(self) -> bool:
        return bool(self.raw[17] & 0x01)

    @property
    def room_temperature(self) -> float:
        return float(self.raw[1])

    @property
    def room_humidity(self) -> int:
        return self.raw[3]

    @property
    def target_temperature(self) -> float:
        return float(self.raw[23] + 16)

    @property
    def mode(self) -> str:
        return MODES.get(self.raw[11], f"0x{self.raw[11]:02x}")

    @property
    def fan(self) -> str:
        return FANS.get(self.raw[13], f"0x{self.raw[13]:02x}")

    @property
    def health(self) -> bool:
        return bool(self.raw[17] & 0x08)

    @property
    def display_off(self) -> bool:
        return bool(self.raw[19] & 0x20)

    @property
    def swing(self) -> int:
        return self.raw[15]

    @property
    def preset(self) -> str:
        b = self.raw[19]
        if b & 0x02:
            return "turbo"
        if b & 0x04:
            return "quiet"
        if self.raw[17] & 0x40:
            return "away"
        return "none"

    # 写（返回新的 24 字节块；cntrl 归 0，与 ESPHome 一致）
    def copy(self) -> "HaierStatus":
        return HaierStatus(bytes(self.raw))

    def with_power(self, on: bool) -> "HaierStatus":
        s = self.copy()
        b = s.raw[17] & ~0x01
        s.raw = s.raw[:17] + bytes([b | (0x01 if on else 0x00)]) + s.raw[18:]
        return s

    def with_target(self, temp: float) -> "HaierStatus":
        if not 16 <= temp <= 30:
            raise ValueError("设定温度支持 16~30℃")
        s = self.copy()
        whole = int(temp)
        half = 1 if (temp - whole) >= 0.5 else 0
        b17 = s.raw[17] & ~0x20
        if half:
            b17 |= 0x20
        s.raw = (s.raw[:17] + bytes([b17]) + s.raw[18:23]
                 + bytes([whole - 16]) + s.raw[24:])
        return s

    def with_mode(self, mode: str) -> "HaierStatus":
        s = self.copy()
        value = MODES_INV[mode]
        s.raw = s.raw[:11] + bytes([value]) + s.raw[12:]
        return s

    def with_fan(self, fan: str) -> "HaierStatus":
        s = self.copy()
        value = FANS_INV[fan]
        s.raw = s.raw[:13] + bytes([value]) + s.raw[14:]
        return s

    def with_swing(self, on: bool) -> "HaierStatus":
        """摆风开关（写 c15）。**同时把 c19 bit0 清 0**。

        实测（2026-10-17）：`c19` bit0 是「改用 c19 的位判摆风」的总闸，App / 遥控器会把它置上；
        一旦置上，主板就忽略 `c15`，我们写摆风会没反应。写入时归零，保证 `c15` 说了算。
        """
        return self.with_byte(15, 1 if on else 0).with_bits(19, 0x01, False)

    def with_health(self, on: bool) -> "HaierStatus":
        return self.with_bits(17, 0x08, on)

    def with_byte(self, index: int, value: int) -> "HaierStatus":
        """按字节下标改（0~23）。"""
        if not 0 <= index < 24:
            raise ValueError("控制块下标必须 0~23")
        s = self.copy()
        s.raw = s.raw[:index] + bytes([value & 0xFF]) + s.raw[index + 1:]
        return s

    def with_bits(self, index: int, mask: int, on: bool) -> "HaierStatus":
        """按位改（掩码置位 / 清位）。"""
        value = self.raw[index] | mask if on else self.raw[index] & ~mask
        return self.with_byte(index, value)

    def for_sending(self) -> bytes:
        """发送用：cntrl 字节必须为 0（与 ESPHome 一致）。"""
        return self.raw[:5] + b"\x00" + self.raw[6:]

    def as_dict(self) -> dict:
        return {
            "power": self.power,
            "room_temperature": self.room_temperature,
            "room_humidity": self.room_humidity,
            "target_temperature": self.target_temperature,
            "mode": self.mode,
            "fan": self.fan,
            "swing": self.swing,
            "health": self.health,
            "display_off": self.display_off,
            "preset": self.preset,
            "raw": self.raw.hex(),
        }


# ----------------------------------------------------------------- 故障位图
def alarm_bits(bitmap: bytes) -> list[int]:
    """把故障位图摊成「置位的位号」。

    位号约定：`位号 = 字节下标 × 8 + 字节内位下标`，**字节内低位在前**（和 c17/c19 那些
    位开关的读法一致）。厂家没公开位图含义，所以这里只给位号 —— 排障时拿它对照、
    报给厂商或社区，别自己猜故障名。
    """
    return [index * 8 + bit for index, byte in enumerate(bitmap)
            for bit in range(8) if (byte >> bit) & 1]

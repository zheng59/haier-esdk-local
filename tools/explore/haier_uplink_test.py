#!/usr/bin/env python3
"""试试模块收不收我们发的帧：几个候选外层封装（只读，用 4d01 读请求，不改空调状态）。

每个候选：新连接 → 收 1.5s 推送 → 发候选 → 等 5s → 报告。
"""
import pathlib
import socket
import struct
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from local_config import HOST as _DEFAULT_HOST, MAC as _DEFAULT_MAC  # noqa: E402  真实值见 local_settings.py

HOST = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_HOST
PORT = 56800
DEV = _DEFAULT_MAC.encode("ascii")
MAGIC = b"\x00\x00\x27\x15"


def epp(data: bytes, ftype: int = 0x01, addr: bytes = b"\x00\x00\x00\x00\x00\x01") -> bytes:
    L = 6 + 1 + len(data) + 1
    body = addr + bytes([ftype]) + data
    return b"\xff\xff" + bytes([L]) + body + bytes([(L + sum(body)) & 0xFF])


def envelop(frame: bytes, magic: bytes = MAGIC, dev: bytes = DEV,
            head_len: int = 79) -> bytes:
    """79 字节头（magic + 36 零 + 12 位设备ID + 余下补零）+ 1 字节总长 + 帧"""
    head = magic.ljust(40, b"\x00")[:40] + dev.ljust(12, b"\x00")[:12]
    head = head.ljust(head_len, b"\x00")
    L = len(frame)
    return head + bytes([L & 0xFF]) + frame


def crc16_haier(frame: bytes) -> bytes:
    """海尔 HaierProtocol 的 CRC16（对 len+payload 累加式 CRC？）先放 CRC-16/MODBUS 试"""
    crc = 0xFFFF
    for b in frame:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack(">H", crc)


def attempt(name: str, payload: bytes, wait: float = 5.0) -> bytes:
    try:
        s = socket.create_connection((HOST, PORT), timeout=5)
    except OSError as e:
        print(f"  [{name}] 连不上: {e}")
        return b""
    try:
        s.settimeout(0.4)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.2:
            try:
                s.recv(4096)
            except socket.timeout:
                continue
        s.sendall(payload)
        got, t0 = b"", time.monotonic()
        while time.monotonic() - t0 < wait:
            try:
                c = s.recv(4096)
                if not c:
                    break
                got += c
                t0 = time.monotonic()
            except socket.timeout:
                continue
        return got
    finally:
        s.close()


def show(name: str, payload: bytes, got: bytes) -> None:
    print(f"\n[{name}] 发送 {len(payload)}B: {payload.hex()}")
    if not got:
        print("  → 无应答")
        return
    print(f"  ← 收到 {len(got)}B: {got.hex()}")
    # 找 EPP 帧
    for i in range(len(got) - 3):
        if got[i + 1:i + 3] == b"\xff\xff" and got[i] > 4:
            L = got[i]
            fr = got[i + 1:i + 1 + 3 + L]
            print(f"    找到帧 @{i}: {fr.hex()}")


def main() -> int:
    req = epp(bytes.fromhex("4d01"))
    req0 = epp(bytes.fromhex("4d01"), addr=b"\x00" * 6)
    cands = [
        ("1 裸 EPP", req),
        ("2 裸 EPP + CRC16", req + crc16_haier(req[2:])),
        ("3 封装 magic2715", envelop(req)),
        ("4 封装 magic2715 addr0", envelop(req0)),
        ("5 封装 magic0000", envelop(req, magic=b"\x00\x00\x00\x00")),
        ("6 封装 无设备ID", envelop(req, dev=b"\x00" * 12)),
        ("7 封装 命令字6d01", envelop(epp(bytes.fromhex("6d01")))),
        ("8 封装 magic6915", envelop(req, magic=b"\x00\x00\x69\x15")),
    ]
    for name, pay in cands:
        got = attempt(name, pay)
        show(name, pay, got)
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""一次性试多条「读」帧，每条一个新会话（带冷却），看空调到底答不答。

用法：python3 haier_probe_all.py 192.168.1.100
"""
import pathlib
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from local_config import HOST as _DEFAULT_HOST, MAC as _DEFAULT_MAC  # noqa: E402  真实值见 local_settings.py

HOST = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_HOST

FRAME_NAMES = {0x01: "CONTROL", 0x02: "STATUS", 0x03: "INVALID", 0x04: "ALARM",
               0x05: "CONFIRM", 0x06: "REPORT", 0x61: "GET_VERSION",
               0x62: "VERSION_RESP", 0x70: "GET_DEV_ID", 0x71: "DEV_ID_RESP",
               0x73: "GET_ALARM", 0x74: "ALARM_RESP", 0x7C: "GET_CONFIG",
               0x7D: "CONFIG_RESP", 0xFC: "GET_MGMT", 0xFD: "MGMT_RESP",
               0xF7: "NET_REPORT"}


def hdr() -> bytes:
    return b"\x00\x00\x27\x15" + b"\x00" * 36 + _DEFAULT_MAC.encode("ascii") + b"\x00" * 27


def epp(data: bytes, ftype: int, addr: bytes = b"\x00\x00\x00\x00\x00\x01") -> bytes:
    L = 6 + 1 + len(data) + 1
    body = addr + bytes([ftype]) + data
    return b"\xff\xff" + bytes([L]) + body + bytes([(L + sum(body)) & 0xFF])


def wrap(frame: bytes) -> bytes:
    return hdr() + bytes([len(frame) & 0xFF]) + frame


def decode(buf: bytes, indent: str = "        ") -> None:
    off = 0
    while off + 80 <= len(buf):
        L3 = buf[off + 79]
        total = 80 + L3
        if len(buf) < total:
            return
        frame = buf[off + 80:off + total]
        if frame[:2] != b"\xff\xff":
            print(f"{indent}⚠ 非 EPP: {frame[:24].hex()}")
        else:
            L = frame[2]
            body = frame[3:3 + L]
            ftype, data, cks = body[6], body[7:-1], body[-1]
            ok = (sum(frame[2:2 + L]) & 0xFF) == cks
            print(f"{indent}· type=0x{ftype:02x}({FRAME_NAMES.get(ftype, '?')}) L={L} "
                  f"addr={body[:6].hex()} cks_ok={ok} data({len(data)})={data.hex()}")
        off += total


def one(name: str, frame: bytes, wait: float = 6.0) -> bool:
    print(f"\n{'─'*66}\n[{name}] 帧 = {frame.hex()}")
    try:
        s = socket.create_connection((HOST, 56800), timeout=8)
    except OSError as e:
        print(f"  连不上: {e}")
        return False
    s.settimeout(0.4)
    burst, t0 = b"", time.monotonic()
    while time.monotonic() - t0 < 1.5:
        try:
            c = s.recv(4096)
        except socket.timeout:
            continue
        if not c:
            print("  推送期间被关闭")
            s.close()
            return False
        burst += c
    print(f"  ① 突发 {len(burst)}B")
    decode(burst)
    s.sendall(wrap(frame))
    t1, got, closed = time.monotonic(), b"", False
    while time.monotonic() - t1 < wait:
        try:
            c = s.recv(4096)
        except socket.timeout:
            continue
        if not c:
            closed = True
            print(f"  [{time.monotonic()-t1:.2f}s] ← 对端关闭")
            break
        print(f"  [{time.monotonic()-t1:.2f}s] ← 收到 {len(c)}B")
        got += c
    if got:
        print("  ③ 应答：")
        decode(got, "        ")
    else:
        print(f"  ③ 无应答{'（连接被关）' if closed else '（连接保持）'}")
    s.close()
    return bool(got)


def main() -> int:
    tests = [
        ("GET_DEVICE_VERSION(0x61) cap=0007", epp(bytes.fromhex("0007"), 0x61)),
        ("GET_ALARM_STATUS(0x73)", epp(b"", 0x73)),
        ("GET_DEVICE_CONFIGURATION(0x7C)", epp(b"", 0x7C)),
        ("GET_MANAGEMENT_INFORMATION(0xFC)", epp(b"", 0xFC)),
        ("CONTROL 4d01 (smartair2 status)", epp(bytes.fromhex("4d01"), 0x01)),
        ("CONTROL 4dfe (big data)", epp(bytes.fromhex("4dfe"), 0x01)),
        ("CONTROL 4c01 (get parameters)", epp(bytes.fromhex("4c01"), 0x01)),
        ("REPORT 6d01 (echo subcommand)", epp(bytes.fromhex("6d01"), 0x06)),
    ]
    hits = []
    for name, fr in tests:
        if one(name, fr):
            hits.append(name)
        time.sleep(3)
    print(f"\n{'='*66}\n有应答的：{hits or '（无）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

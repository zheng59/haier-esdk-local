#!/usr/bin/env python3
"""带冷却的批试探：不同「目标地址」+ 地址发现命令（只读）。

用法：python3 haier_batch2.py 192.168.1.100
"""
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
NAMES = {0x01: "CONTROL", 0x02: "STATUS", 0x03: "INVALID", 0x04: "ALARM",
         0x05: "CONFIRM", 0x06: "REPORT", 0x61: "GET_VERSION", 0x62: "VERSION_RESP",
         0x67: "GET_ADDRESSES", 0x68: "ADDR_RESP", 0x73: "GET_ALARM", 0x74: "ALARM_RESP"}


def hdr() -> bytes:
    return b"\x00\x00\x27\x15" + b"\x00" * 36 + b"001122334455" + b"\x00" * 27


def epp(data: bytes, ftype: int, addr: bytes) -> bytes:
    L = 6 + 1 + len(data) + 1
    body = addr + bytes([ftype]) + data
    return b"\xff\xff" + bytes([L]) + body + bytes([(L + sum(body)) & 0xFF])


def run(name: str, frame: bytes, cooldown: float = 8.0, wait: float = 6.0) -> None:
    print(f"\n{'─'*66}\n[{name}] {frame.hex()}")
    time.sleep(cooldown)
    try:
        s = socket.create_connection((HOST, 56800), timeout=8)
    except OSError as e:
        print(f"  连不上：{e}")
        return
    s.settimeout(0.4)
    burst, t0 = b"", time.monotonic()
    while time.monotonic() - t0 < 1.5:
        try:
            c = s.recv(4096)
        except socket.timeout:
            continue
        if not c:
            print("  推送期间被关闭（模块还有残留会话/限流）")
            s.close()
            return
        burst += c
    print(f"  ① 突发 {len(burst)}B（{'正常' if len(burst) == 216 else '异常'}）")
    if len(burst) != 216:
        s.close()
        return
    s.sendall(hdr() + bytes([len(frame) & 0xFF]) + frame)
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
        print(f"  [{time.monotonic()-t1:.2f}s] ← 收到 {len(c)}B: {c[-40:].hex()}")
        got += c
    if not got:
        print(f"  ③ 无应答（{'连接被关' if closed else '连接保持'}）")
    s.close()


def main() -> int:
    runs = []
    for addr_hex in ["000000000002", "0000000000ff", "410203040506", "000000000000"]:
        runs.append((f"CONTROL 4d01 addr={addr_hex}",
                     epp(bytes.fromhex("4d01"), 0x01, bytes.fromhex(addr_hex))))
    runs.append(("GET_ALL_ADDRESSES 0x67 addr=00..00",
                 epp(b"", 0x67, bytes.fromhex("000000000000"))))
    runs.append(("GET_ALL_ADDRESSES 0x67 addr=00..01",
                 epp(b"", 0x67, bytes.fromhex("000000000001"))))
    runs.append(("GET_DEVICE_VERSION 0x61 addr=00..00 data=0007",
                 epp(bytes.fromhex("0007"), 0x61, bytes.fromhex("000000000000"))))
    runs.append(("CONTROL 4d01 addr=00..01（对照）",
                 epp(bytes.fromhex("4d01"), 0x01, bytes.fromhex("000000000001"))))
    for name, fr in runs:
        run(name, fr)
    print("\n完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())

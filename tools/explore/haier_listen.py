#!/usr/bin/env python3
"""只连上去听，什么都不发，看模块自己什么时候说话（时间戳 + 解析）。

用法：python3 haier_listen.py 192.168.1.100 [秒数]
"""
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0


def split_messages(buf: bytes) -> list[bytes]:
    """外层：79 字节头 + 1 字节总长 + EPP 帧。"""
    out, off = [], 0
    while off + 80 <= len(buf):
        L = buf[off + 79]
        total = 80 + L
        if off + total > len(buf):
            break
        out.append(buf[off:off + total])
        off += total
    return out, off


def desc(msg: bytes) -> str:
    frame = msg[79:]
    if frame[:2] != b"\xff\xff":
        return f"非 EPP: {frame[:24].hex()}"
    L = frame[2]
    body = frame[3:3 + L]
    ftype, data, cks = body[6], body[7:-1], body[-1]
    ok = (sum(frame[2:2 + L]) & 0xFF) == cks
    names = {0x01: "CONTROL", 0x02: "STATUS", 0x03: "INVALID", 0x04: "ALARM",
             0x05: "CONFIRM", 0x06: "REPORT", 0x61: "GET_VERSION"}
    s = (f"EPP type=0x{ftype:02x}({names.get(ftype, '?')}) L={L} "
         f"addr={body[:6].hex()} cks_ok={ok} data({len(data)})={data.hex()}")
    if ftype in (0x02, 0x06) and len(data) >= 26:
        c = data[2:26]
        s += (f"\n      开关={'开' if c[17] & 1 else '关'} 室温={c[1]}°C 湿度={c[3]}% "
              f"设定={c[23] + 16}°C 模式=0x{c[11]:02x} 风速=0x{c[13]:02x}")
    return s


s = socket.create_connection((HOST, 56800), timeout=10)
s.settimeout(0.5)
buf, t0 = b"", time.monotonic()
print(f"连接成功，监听 {SECS}s …")
try:
    while time.monotonic() - t0 < SECS:
        try:
            c = s.recv(4096)
        except socket.timeout:
            continue
        if not c:
            print(f"[{time.monotonic()-t0:6.2f}s] 对端关闭连接")
            break
        buf += c
        print(f"[{time.monotonic()-t0:6.2f}s] 收到 {len(c)} 字节（累计 {len(buf)}）")
        msgs, used = split_messages(buf)
        for m in msgs:
            print(f"           - {desc(m)}")
        if used:
            buf = buf[used:]
finally:
    s.close()
print(f"结束，共 {len(buf)} 字节残留: {buf.hex()[:200]}")

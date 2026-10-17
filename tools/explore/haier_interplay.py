#!/usr/bin/env python3
"""交互探测：连上 → 等推送 → 发候选 → 只看「推送之后」的增量（只读请求）。

用法：python3 haier_interplay.py 192.168.1.100
"""
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
PORT = 56800
DEV = b"001122334455"

FRAME_NAMES = {0x01: "CONTROL", 0x02: "STATUS", 0x03: "INVALID", 0x04: "ALARM",
               0x05: "CONFIRM", 0x06: "REPORT", 0x61: "GET_VERSION"}


def epp(data: bytes, ftype: int = 0x01, addr: bytes = b"\x00\x00\x00\x00\x00\x01") -> bytes:
    L = 6 + 1 + len(data) + 1
    body = addr + bytes([ftype]) + data
    return b"\xff\xff" + bytes([L]) + body + bytes([(L + sum(body)) & 0xFF])


def envelope(frame: bytes, magic: bytes = b"\x00\x00\x27\x15", dev: bytes = DEV) -> bytes:
    head = (magic.ljust(40, b"\x00")[:40] + dev.ljust(12, b"\x00")[:12]).ljust(79, b"\x00")
    return head + bytes([frame[2] if frame[:2] == b"\xff\xff" else len(frame)]) + frame


def parse_all(buf: bytes) -> list[str]:
    """按 79 字节头解析所有完整消息。"""
    out, off = [], 0
    while off + 80 <= len(buf):
        head = buf[off:off + 79]
        L = buf[off + 79] - 3          # 封装的长度字节 = EPP 总长 = 3 + L
        frame = buf[off + 80:off + 80 + 3 + L]
        if L < 0 or len(frame) < 3 + L or frame[:2] != b"\xff\xff":
            out.append(f"⚠ 偏移 {off} 解析失败: {buf[off:off+24].hex()}")
            break
        body = frame[3:3 + L]
        ftype, data, cks = body[6], body[7:-1], body[-1]
        ok = (sum(frame[2:2 + L]) & 0xFF) == cks
        dev = head[40:52].decode("ascii", "replace")
        line = (f"dev={dev} type=0x{ftype:02x}({FRAME_NAMES.get(ftype, '?')}) "
                f"addr={body[:6].hex()} cks_ok={ok} data={data.hex()}")
        if ftype in (0x02, 0x06) and len(data) >= 26:
            c = data[2:26]
            line += (f"\n         → 开关={'开' if c[17] & 1 else '关'} 室温={c[1]}°C "
                     f"湿度={c[3]}% 设定={c[23] + 16}°C 模式=0x{c[11]:02x} 风速=0x{c[13]:02x}")
        out.append(line)
        off += 80 + 3 + L
    return out


def session(name: str, payload: bytes | None, push_wait: float = 12.0,
            reply_wait: float = 10.0) -> None:
    print(f"\n{'='*70}\n[{name}]")
    try:
        s = socket.create_connection((HOST, PORT), timeout=8)
    except OSError as e:
        print(f"  连不上: {e}")
        return
    push = b""
    t0 = time.monotonic()
    try:
        s.settimeout(0.4)
        # ① 等推送
        while time.monotonic() - t0 < push_wait:
            try:
                c = s.recv(4096)
            except socket.timeout:
                continue
            if not c:
                print("  对端关闭")
                return
            push += c
            if len(push) >= 99:   # 至少第一条消息齐了
                break
        print(f"  ① 推送 {len(push)}B（{time.monotonic()-t0:.2f}s）")
        for l in parse_all(push):
            print(f"     {l}")
        if payload is None:
            return
        # ② 发候选
        print(f"  ② 发送 {len(payload)}B: {payload.hex()}")
        s.sendall(payload)
        t1 = time.monotonic()
        got = b""
        while time.monotonic() - t1 < reply_wait:
            try:
                c = s.recv(4096)
            except socket.timeout:
                continue
            if not c:
                print(f"  ← 对端关闭（{time.monotonic()-t1:.2f}s）")
                break
            got += c
            print(f"  ← 收到 {len(c)}B（增量累计 {len(got)}）")
            t1 = time.monotonic()
        if not got:
            print("  → 增量：无（没有应答）")
        else:
            for l in parse_all(got):
                print(f"     {l}")
    finally:
        s.close()


def main() -> int:
    req1 = epp(bytes.fromhex("4d01"), addr=b"\x00\x00\x00\x00\x00\x01")
    req0 = epp(bytes.fromhex("4d01"), addr=b"\x00" * 6)
    session("基线：只收不发", None)
    time.sleep(4)
    session("A 裸 EPP 4d01 addr=…01", req1)
    time.sleep(4)
    session("B 裸 EPP 4d01 addr=…00", req0)
    time.sleep(4)
    session("C 封装 4d01 addr=…01", envelope(req1))
    time.sleep(4)
    session("D 封装 4d01 addr=…00", envelope(req0))
    return 0


if __name__ == "__main__":
    sys.exit(main())

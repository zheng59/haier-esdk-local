#!/usr/bin/env python3
"""按正确的封装格式发帧给空调（先只发只读请求）。

封装 = [79 字节描述头] + [1 字节 EPP 总长 = 3 + L] + [EPP 帧]
用法：
  python3 haier_send.py 192.168.1.100 4d01            # 读状态请求
  python3 haier_send.py 192.168.1.100 4d01 --addr 000000000000
  python3 haier_send.py 192.168.1.100 --raw-hex ffff0a000000000001014d015a
"""
import argparse
import socket
import sys
import time

FRAME_NAMES = {0x01: "CONTROL", 0x02: "STATUS", 0x03: "INVALID", 0x04: "ALARM",
               0x05: "CONFIRM", 0x06: "REPORT", 0x61: "GET_VERSION",
               0x62: "VERSION_RESP", 0x73: "GET_ALARM", 0x74: "ALARM_RESP",
               0x7C: "GET_CONFIG", 0x7D: "CONFIG_RESP", 0xF7: "NET_REPORT"}


def hdr() -> bytes:
    return b"\x00\x00\x27\x15" + b"\x00" * 36 + b"001122334455" + b"\x00" * 27


def epp(data: bytes, ftype: int = 0x01, addr: bytes = b"\x00\x00\x00\x00\x00\x01") -> bytes:
    L = 6 + 1 + len(data) + 1
    body = addr + bytes([ftype]) + data
    return b"\xff\xff" + bytes([L]) + body + bytes([(L + sum(body)) & 0xFF])


def wrap(frame: bytes) -> bytes:
    return hdr() + bytes([len(frame) & 0xFF]) + frame


def decode(buf: bytes) -> None:
    off = 0
    while off + 80 <= len(buf):
        L3 = buf[off + 79]
        total = 80 + L3
        if len(buf) < total:
            print(f"      （不完整，剩 {len(buf)-off}B）")
            return
        msg = buf[off:off + total]
        frame = msg[80:]
        if frame[:2] != b"\xff\xff":
            print(f"      ⚠ 非 EPP: {frame[:24].hex()}")
        else:
            L = frame[2]
            body = frame[3:3 + L]
            ftype, data, cks = body[6], body[7:-1], body[-1]
            ok = (sum(frame[2:2 + L]) & 0xFF) == cks
            print(f"      · type=0x{ftype:02x}({FRAME_NAMES.get(ftype, '?')}) L={L} "
                  f"addr={body[:6].hex()} cks_ok={ok} data({len(data)})={data.hex()}")
            if ftype in (0x02, 0x06) and len(data) >= 26:
                c = data[2:26]
                print(f"        → 开关={'开' if c[17] & 1 else '关'} 室温={c[1]}°C "
                      f"湿度={c[3]}% 设定={c[23] + 16}°C 模式=0x{c[11]:02x} 风速=0x{c[13]:02x}")
        off += total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("word", nargs="?", help="命令字（如 4d01）")
    ap.add_argument("--addr", default="000000000001")
    ap.add_argument("--type", default="01", help="帧类型，默认 01 (CONTROL)")
    ap.add_argument("--raw-hex", help="直接给完整 EPP 帧 hex（内部不做封装）")
    ap.add_argument("--secs", type=float, default=10.0)
    args = ap.parse_args()

    frame = (bytes.fromhex(args.raw_hex) if args.raw_hex
             else epp(bytes.fromhex(args.word), int(args.type, 16), bytes.fromhex(args.addr)))
    pay = wrap(frame)

    s = socket.create_connection((args.host, 56800), timeout=8)
    s.settimeout(0.4)
    burst, t0 = b"", time.monotonic()
    while time.monotonic() - t0 < 1.5:
        try:
            c = s.recv(4096)
        except socket.timeout:
            continue
        if not c:
            print("推送期间被关闭")
            return 1
        burst += c
    print(f"① 连接突发 {len(burst)}B：")
    decode(burst)
    print(f"② 发送 {len(pay)}B：hdr + {len(frame)}B 帧 = {pay[-len(frame):].hex()}")
    s.sendall(pay)
    t1, got = time.monotonic(), b""
    while time.monotonic() - t1 < args.secs:
        try:
            c = s.recv(4096)
        except socket.timeout:
            continue
        if not c:
            print(f"[{time.monotonic()-t1:.2f}s] ← 对端关闭")
            break
        print(f"[{time.monotonic()-t1:.2f}s] ← 收到 {len(c)}B")
        got += c
    if got:
        print("③ 应答：")
        decode(got)
    else:
        print("③ 没有收到任何数据")
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""长监听 + 可选发送：看清「连接突发」到底有几条、什么时候来、发送后有没有新东西。

用法：
  python3 haier_watch.py 192.168.1.100                # 只听 30s
  python3 haier_watch.py 192.168.1.100 --send 61      # 收到突发后发一条
  python3 haier_watch.py 192.168.1.100 --send-hex ffff0a000000000001014d015a
"""
import argparse
import pathlib
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from local_config import HOST as _DEFAULT_HOST  # noqa: E402  真实值见 local_settings.py

FRAME_NAMES = {0x01: "CONTROL", 0x02: "STATUS", 0x03: "INVALID", 0x04: "ALARM",
               0x05: "CONFIRM", 0x06: "REPORT", 0x61: "GET_VERSION",
               0x62: "VERSION_RESP", 0x73: "GET_ALARM", 0x74: "ALARM_RESP",
               0x7C: "GET_CONFIG", 0x7D: "CONFIG_RESP", 0xF7: "NET_REPORT"}


def epp(data: bytes, ftype: int = 0x01, addr: bytes = b"\x00\x00\x00\x00\x00\x01") -> bytes:
    L = 6 + 1 + len(data) + 1
    body = addr + bytes([ftype]) + data
    return b"\xff\xff" + bytes([L]) + body + bytes([(L + sum(body)) & 0xFF])


def decode_msg(msg: bytes) -> str:
    if len(msg) < 80:
        return f"短报文 {msg.hex()}"
    frame = msg[80:]
    if frame[:2] != b"\xff\xff" or len(frame) < 4:
        return f"非 EPP：{frame.hex()}"
    L = frame[2]
    if len(frame) < 3 + L:
        return f"不完整 EPP：{frame.hex()}"
    body = frame[3:3 + L]
    ftype, data, cks = body[6], body[7:-1], body[-1]
    ok = (sum(frame[2:2 + L]) & 0xFF) == cks
    dev = msg[40:52].decode("ascii", "replace")
    s = (f"dev={dev} type=0x{ftype:02x}({FRAME_NAMES.get(ftype, '?')}) L={L} "
         f"addr={body[:6].hex()} cks_ok={ok}\n      data({len(data)})={data.hex()}")
    if ftype in (0x02, 0x06) and len(data) >= 26:
        c = data[2:26]
        s += (f"\n      → 开关={'开' if c[17] & 1 else '关'} 室温={c[1]}°C 湿度={c[3]}% "
              f"设定={c[23] + 16}°C 模式=0x{c[11]:02x} 风速=0x{c[13]:02x} c5=0x{c[5]:02x}")
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?", default=_DEFAULT_HOST)
    ap.add_argument("--secs", type=float, default=30.0)
    ap.add_argument("--send", help="突发后要发的命令字（如 61、4d01）")
    ap.add_argument("--send-hex", help="突发后要发的完整 hex")
    ap.add_argument("--send-type", default="01", help="--send 时的帧类型（默认 01）")
    ap.add_argument("--send-addr", default="000000000001")
    args = ap.parse_args()

    s = socket.create_connection((args.host, 56800), timeout=8)
    s.settimeout(0.4)
    buf, sent, t0, last = b"", False, time.monotonic(), 0
    print(f"连接成功，观察 {args.secs}s …")
    try:
        while time.monotonic() - t0 < args.secs:
            try:
                c = s.recv(4096)
            except socket.timeout:
                continue
            if not c:
                print(f"[{time.monotonic()-t0:6.2f}s] ← 对端关闭连接")
                break
            buf += c
            print(f"[{time.monotonic()-t0:6.2f}s] +{len(c)}B（累计 {len(buf)}）")
            # 解析已完整的消息
            while len(buf) >= 80:
                L3 = buf[79]
                total = 80 + L3
                if len(buf) < total:
                    break
                print(f"      · {decode_msg(buf[:total])}")
                buf = buf[total:]
                last = time.monotonic()
            if not sent and args.secs and last:
                pass
            if (args.send or args.send_hex) and not sent and last:
                pay = (bytes.fromhex(args.send_hex) if args.send_hex
                       else epp(bytes.fromhex(args.send), int(args.send_type, 16),
                                bytes.fromhex(args.send_addr)))
                print(f"[{time.monotonic()-t0:6.2f}s] → 发送 {len(pay)}B: {pay.hex()}")
                s.sendall(pay)
                sent = True
    finally:
        s.close()
    print(f"结束（残留 {len(buf)}B: {buf.hex()[:80]}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

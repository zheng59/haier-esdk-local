#!/usr/bin/env python3
"""按 KZW-W002 逆向出的「请求」封装格式发帧（这版才是对的）。

请求封装（80 字节头 + EPP 帧）：
  00 00 27 14 | 36 个 0 | MAC(12 ASCII) | 4 个 0 | 16 个 0 | 序号(BE32) | 帧长(BE32) | 帧
（响应方向反过来是 00 00 27 15，其余一样）

用法：
  python3 haier_req.py 192.168.1.100 hello      # 4d01 读状态（addr 00）
  python3 haier_req.py 192.168.1.100 poll       # 4d01 读状态（addr 01）
  python3 haier_req.py 192.168.1.100 init       # 0x73 读报警状态
  python3 haier_req.py 192.168.1.100 on         # 4d02 开机（会改状态！）
  python3 haier_req.py 192.168.1.100 off        # 4d03 关机（会改状态！）
  python3 haier_req.py 192.168.1.100 --frame <完整 EPP 帧 hex>
"""
import argparse
import pathlib
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from local_config import HOST as _DEFAULT_HOST, MAC as _DEFAULT_MAC  # noqa: E402  真实值见 local_settings.py

NAMES = {0x01: "CONTROL", 0x02: "STATUS", 0x03: "INVALID", 0x04: "ALARM", 0x05: "CONFIRM",
         0x06: "REPORT", 0x61: "GET_VERSION", 0x62: "VERSION_RESP", 0x73: "GET_ALARM",
         0x74: "ALARM_RESP", 0x7C: "GET_CONFIG", 0x7D: "CONFIG_RESP"}

CMD = {
    "hello": "ffff0a000000000000014d0159",
    "poll":  "ffff0a000000000001014d015a",
    "init":  "ffff08000000000000737b",
    "on":    "ffff0a000000000000014d025a",
    "off":   "ffff0a000000000000014d035b",
}


def build(frame: bytes, mac: bytes = _DEFAULT_MAC.encode("ascii"), seq: int = 1) -> bytes:
    head = (b"\x00\x00\x27\x14" + b"\x00" * 36 + mac.ljust(12, b"\x00")[:12]
            + b"\x00" * 4 + b"\x00" * 16
            + seq.to_bytes(4, "big") + len(frame).to_bytes(4, "big"))
    return head + frame


def decode(buf: bytes, indent: str = "    ") -> bool:
    off, found = 0, False
    while off + 84 <= len(buf):
        if buf[off:off + 4] not in (b"\x00\x00\x27\x15", b"\x00\x00\x27\x14"):
            off += 1
            continue
        length = int.from_bytes(buf[off + 76:off + 80], "big")
        total = 80 + length
        if off + total > len(buf) or length <= 0:
            break
        frame = buf[off + 80:off + total]
        if frame[:2] != b"\xff\xff":
            print(f"{indent}⚠ 非 EPP: {frame[:24].hex()}")
            off += total
            continue
        L = frame[2]
        body = frame[3:3 + L]
        ftype, data, cks = body[6], body[7:-1], body[-1]
        ok = (sum(frame[2:2 + L]) & 0xFF) == cks
        print(f"{indent}· 序号={int.from_bytes(buf[off+72:off+76],'big')} 长度={length} "
              f"type=0x{ftype:02x}({NAMES.get(ftype,'?')}) L={L} addr={body[:6].hex()} "
              f"cks_ok={ok}\n{indent}  data({len(data)})={data.hex()}")
        if ftype in (0x02, 0x06) and len(data) >= 26:
            c = data[2:26]
            modes = {0x00: "自动", 0x01: "制冷", 0x02: "制热", 0x03: "送风", 0x04: "除湿"}
            fans = {0x00: "高", 0x01: "中", 0x02: "低", 0x03: "自动"}
            print(f"{indent}  → 开关={'开' if c[17] & 1 else '关'} 室温={c[1]}°C 湿度={c[3]}% "
                  f"设定={c[23] + 16}°C 模式=0x{c[11]:02x}({modes.get(c[11],'?')}) "
                  f"风速=0x{c[13]:02x}({fans.get(c[13],'?')})")
        found = True
        off += total
    return found


def main() -> int:
    argv = sys.argv[1:]
    # 允许省略 IP：`haier_req.py hello` 等于 `haier_req.py <local_settings 里的 IP> hello`
    if argv and argv[0] in CMD:
        argv = [_DEFAULT_HOST, *argv]

    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?", default=_DEFAULT_HOST)
    ap.add_argument("cmd", nargs="?", choices=list(CMD), help="预设命令")
    ap.add_argument("--frame", help="完整 EPP 帧 hex")
    ap.add_argument("--seq", type=int, default=1)
    ap.add_argument("--secs", type=float, default=8.0)
    args = ap.parse_args(argv)

    frame = bytes.fromhex(args.frame) if args.frame else bytes.fromhex(CMD[args.cmd])
    req = build(frame, seq=args.seq)
    print(f"请求 {len(req)}B: {req.hex()}")

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
    print("② 发送请求 …")
    s.sendall(req)
    t1, got, closed = time.monotonic(), b"", False
    while time.monotonic() - t1 < args.secs:
        try:
            c = s.recv(4096)
        except socket.timeout:
            continue
        if not c:
            closed = True
            print(f"[{time.monotonic()-t1:.2f}s] ← 对端关闭")
            break
        print(f"[{time.monotonic()-t1:.2f}s] ← 收到 {len(c)}B")
        got += c
    print("③ 应答：")
    if got and not decode(got, "    "):
        print(f"    （无法解析）{got.hex()}")
    elif not got:
        print(f"    无应答（{'被关闭' if closed else '连接保持'}）")
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

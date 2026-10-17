#!/usr/bin/env python3
"""海尔 eSDK 模块 TCP 56800 明文 EPP 直连（只读版）。

帧格式（已验证）：
    外层 = [38 字节头，含设备 ID] + <1 字节 EPP 总长>  ... 周界见 haier_raw.py
    EPP  = ff ff | L | addr(6) | type(1) | data | checksum
           L = 6 + 1 + len(data) + 1          （checksum 计入 L）
           checksum = (L + addr + type + data) & 0xFF     （累加和，不是补码）

帧类型（EPP）：
    0x01 CONTROL（读请求 / 设置命令）
    0x02 STATUS（对 CONTROL 的应答）
    0x04 ALARM_STATUS（故障位图，命令字 0f5a）
    0x06 REPORT（设备主动上报，状态命令字 6d01）

用法：
    python3 haier_local.py 192.168.1.100              # 只读：收推送 + 发 4d01 读请求
    python3 haier_local.py 192.168.1.100 --no-request # 只看推送
"""
import argparse
import socket
import struct
import sys
import time

PORT = 56800
TYPE_CONTROL = 0x01
TYPE_STATUS = 0x02
TYPE_ALARM = 0x04
TYPE_REPORT = 0x06

# smartair2 口径的字段表（data 的前 2 字节是命令字）
SMARTAIR2_MODES = {0x00: "自动", 0x01: "制冷", 0x02: "制热", 0x03: "送风", 0x04: "除湿"}
SMARTAIR2_FANS = {0x00: "高", 0x01: "中", 0x02: "低", 0x03: "自动"}


def epp_frame(data: bytes, ftype: int = TYPE_CONTROL, addr: bytes = b"\x00" * 6) -> bytes:
    L = 6 + 1 + len(data) + 1
    body = addr + bytes([ftype]) + data
    checksum = (L + sum(body)) & 0xFF
    return b"\xff\xff" + bytes([L]) + body + bytes([checksum])


def parse_epp(seg: bytes) -> dict | None:
    """seg 以 ff ff 开头。"""
    if len(seg) < 4 or seg[:2] != b"\xff\xff":
        return None
    L = seg[2]
    total = 3 + L
    if len(seg) < total:
        return None
    body = seg[3:total]
    data = body[7:-1]
    return {
        "total": total,
        "L": L,
        "addr": body[:6].hex(),
        "type": body[6],
        "data": data,
        "checksum": body[-1],
        "checksum_ok": (sum(seg[2:total - 1]) & 0xFF) == body[-1],
        "raw": seg[:total],
    }


def find_frames(buf: bytes) -> list[dict]:
    """在外层流里找所有 'L ff ff'，并解析后面的 EPP 帧（去重）。"""
    out, seen = [], set()
    for i in range(len(buf) - 3):
        if buf[i + 1:i + 3] == b"\xff\xff" and buf[i] > 4:
            fr = parse_epp(buf[i + 1:])
            if fr and fr["L"] == buf[i] and fr["total"] <= len(buf) - i - 1:
                key = (i, fr["total"])
                if key not in seen:
                    seen.add(key)
                    fr["offset"] = i
                    out.append(fr)
    return out


def capture(sock: socket.socket, wait: float) -> bytes:
    buf, t0 = b"", time.monotonic()
    sock.settimeout(0.4)
    while time.monotonic() - t0 < wait:
        try:
            c = sock.recv(4096)
            if not c:
                break
            buf += c
        except socket.timeout:
            continue
    return buf


def describe_smartair2(data: bytes) -> list[str]:
    """按 smartair2 口径解（data = 命令字2 + 控制块24）。"""
    if len(data) < 26:
        return [f"（{len(data)} 字节，不够 smartair2 的 26 字节）"]
    cmd = struct.unpack(">H", data[:2])[0]
    c = data[2:26]
    power = "开" if (c[17] & 0x01) else "关"
    lines = [
        f"命令字        = 0x{cmd:04x}",
        f"开关          = {power}",
        f"室温          = {c[1]}°C",
        f"湿度          = {c[3]}%",
        f"设定温度      = {c[23] + 16}°C"
        + ("（半度位）" if (c[17] & 0x20) else ""),
        f"模式          = 0x{c[11]:02x} {SMARTAIR2_MODES.get(c[11], '?')}",
        f"风速          = 0x{c[13]:02x} {SMARTAIR2_FANS.get(c[13], '?')}",
        f"摆风          = 0x{c[15]:02x}",
        f"健康          = {'开' if (c[17] & 0x08) else '关'}",
        f"显示          = {'关' if (c[19] & 0x20) else '开'}",
        f"极速/静音/睡眠= {c[19] & 0x03:#04x} / {c[17] & 0x40:#04x} / {c[19] & 0x04:#04x}",
        f"cntrl 字节    = 0x{c[5]:02x}",
        f"原始控制块    = {c.hex()}",
    ]
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--no-request", action="store_true", help="只收推送，不发读请求")
    ap.add_argument("--addr", default="000000000001", help="6 字节地址（默认 000000000001）")
    ap.add_argument("--wait", type=float, default=3.0)
    ap.add_argument("--request-word", default="4d01", help="读请求命令字（默认 4d01）")
    args = ap.parse_args()

    s = socket.create_connection((args.host, PORT), timeout=5)
    try:
        print("① 收设备主动推送 …")
        buf = capture(s, 1.5)
        print(f"   {len(buf)} 字节")
        frames = find_frames(buf)
        for fr in frames:
            print(f"\n   [偏移 0x{fr['offset']:03x}] L={fr['L']} type=0x{fr['type']:02x} "
                  f"addr={fr['addr']} checksum=0x{fr['checksum']:02x} ok={fr['checksum_ok']}")
            print(f"      data({len(fr['data'])}) = {fr['data'].hex()}")
            if fr["type"] in (TYPE_REPORT, TYPE_STATUS) and len(fr["data"]) >= 26:
                for line in describe_smartair2(fr["data"]):
                    print(f"      {line}")
            elif fr["type"] == TYPE_ALARM:
                bitmap = fr["data"][2:]
                print(f"      故障位图 = {bitmap.hex()}（全 0 = 无故障）")
        if not frames:
            print("    （没解析出 EPP 帧，原始：%s）" % buf.hex())

        if args.no_request:
            return 0

        word = bytes.fromhex(args.request_word)
        req = epp_frame(word, TYPE_CONTROL, bytes.fromhex(args.addr))
        print(f"\n② 发只读请求（type=CONTROL，命令字 0x{args.request_word}，addr={args.addr}）")
        print(f"   {req.hex()}")
        s.sendall(req)

        print("\n③ 等应答 …")
        buf2 = capture(s, args.wait)
        print(f"   {len(buf2)} 字节")
        for fr in find_frames(buf2):
            print(f"\n   [偏移 0x{fr['offset']:03x}] L={fr['L']} type=0x{fr['type']:02x} "
                  f"addr={fr['addr']} checksum=0x{fr['checksum']:02x} ok={fr['checksum_ok']}")
            print(f"      data({len(fr['data'])}) = {fr['data'].hex()}")
            if fr["type"] in (TYPE_REPORT, TYPE_STATUS) and len(fr["data"]) >= 26:
                for line in describe_smartair2(fr["data"]):
                    print(f"      {line}")
        if not find_frames(buf2):
            print("   （没有解析出 EPP 帧，原始：%s）" % buf2.hex())
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

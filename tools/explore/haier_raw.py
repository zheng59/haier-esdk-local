#!/usr/bin/env python3
"""原始字节探测器：把某个 TCP 端口上收到的东西原样摊开（只读）。

用法：
  python3 haier_raw.py 192.168.1.100 56800 --listen        # 连上后只听 3 秒
  python3 haier_raw.py 192.168.1.100 56800 --uss-hello     # 发标准 uSS HELLO
  python3 haier_raw.py 192.168.1.100 12334 --uss-hello
  python3 haier_raw.py 192.168.1.100 56800 --hex 4861696572 # 发自定义字节
"""
import argparse
import socket
import struct
import sys
import time

MAGIC = b"Haier"
CMD_SEARCH = 0x6915


def udiscovery_request() -> bytes:
    payload = bytearray(56)
    payload[0x10:0x15] = b"2.0.0"
    payload[0x18:0x18 + len(b"UDISCOVERY_SDK")] = b"UDISCOVERY_SDK"
    return (MAGIC + struct.pack(">I", CMD_SEARCH) + struct.pack(">H", 0x020A)
            + b"\x00" * 6 + struct.pack(">I", len(payload)) + bytes(payload))


def uss_hello(device_id: str = "001122334455", pro_ver: int = 2) -> bytes:
    type_byte = {2: 0x01, 3: 0x6E}[pro_ver]
    dev = device_id.encode("ascii").ljust(32, b"\x00")[:32]
    return struct.pack(">IHBBIHH", 0xEA60, len(dev) + 0x0A, type_byte, 0, 1, 0, 0) + dev


def dump(tag: str, data: bytes) -> None:
    print(f"  {tag}: {len(data)} 字节")
    if not data:
        return
    print(f"    hex   = {data.hex()}")
    for i in range(0, len(data), 32):
        chunk = data[i:i + 32]
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"    {i:04x}  {chunk.hex(' '):<96}  |{printable}|")


def ascii_fields(data: bytes, minlen: int = 4) -> None:
    """把连续的可打印字节串找出来（看看里面有哪些文本字段）。"""
    runs, cur = [], bytearray()
    for b in data:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                runs.append(bytes(cur))
            cur = bytearray()
    if len(cur) >= minlen:
        runs.append(bytes(cur))
    if runs:
        print("    可打印片段：")
        for r in runs:
            print(f"      @? {r.decode('ascii')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--listen", action="store_true", help="连上后只听不回")
    ap.add_argument("--uss-hello", action="store_true", help="发标准 uSS HELLO")
    ap.add_argument("--udiscovery", action="store_true", help="发 UDISCOVERY 请求")
    ap.add_argument("--hex", help="发送指定 hex 字节")
    ap.add_argument("--wait", type=float, default=3.0, help="等待秒数")
    args = ap.parse_args()

    try:
        s = socket.create_connection((args.host, args.port), timeout=args.wait)
    except OSError as e:
        print(f"连接失败：{e}")
        return 1

    try:
        s.settimeout(0.6)
        # 先看设备有没有主动打招呼
        pre = b""
        t0 = time.monotonic()
        while time.monotonic() - t0 < min(args.wait, 1.5):
            try:
                c = s.recv(4096)
                if not c:
                    break
                pre += c
            except socket.timeout:
                break
        if pre:
            print("① 连上后设备主动发来的：")
            dump("recv", pre)
            ascii_fields(pre)
        else:
            print("① 连上后设备没有主动说话")

        send = None
        if args.uss_hello:
            send = uss_hello()
        elif args.udiscovery:
            send = udiscovery_request()
        elif args.hex:
            send = bytes.fromhex(args.hex.replace(" ", ""))
        if send is None:
            return 0

        print(f"② 发送 {len(send)} 字节：{send.hex()}")
        s.sendall(send)
        s.settimeout(0.6)
        got = b""
        t0 = time.monotonic()
        while time.monotonic() - t0 < args.wait:
            try:
                c = s.recv(4096)
                if not c:
                    print("   （对端关闭了连接）")
                    break
                got += c
            except socket.timeout:
                continue
        if got:
            dump("recv", got)
            ascii_fields(got)
        else:
            print("③ 没有回应")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""模块对「收包」的反应测试（只读）：
   idle  : 什么都不发
   echo  : 把它自己推来的 216 字节原样发回去
   one   : 只发 1 个 0x00 字节
   epflen: 发「1 字节总长 + EPP 帧」（不带 79 字节描述头）
用法：python3 haier_react.py 192.168.1.100 echo
"""
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
MODE = sys.argv[2] if len(sys.argv) > 2 else "idle"


def hdr() -> bytes:
    return (b"\x00\x00\x27\x15" + b"\x00" * 36 + b"001122334455" + b"\x00" * 27)


def main() -> int:
    s = socket.create_connection((HOST, 56800), timeout=8)
    s.settimeout(0.4)
    burst, t0 = b"", time.monotonic()
    while time.monotonic() - t0 < 1.5:
        try:
            c = s.recv(4096)
        except socket.timeout:
            continue
        if not c:
            print("对端在推送期间就关闭了")
            return 1
        burst += c
    print(f"推送 {len(burst)}B")

    paid = None
    if MODE == "echo":
        paid = burst
    elif MODE == "one":
        paid = b"\x00"
    elif MODE == "epflen":
        frame = bytes.fromhex("ffff10000000000001040f5a0000000000007e")
        paid = bytes([len(frame)]) + frame
    elif MODE == "idle":
        paid = None

    if paid:
        print(f"→ 发送 {len(paid)}B: {paid.hex()[:120]}")
        s.sendall(paid)
    print("观察 12s …")
    t1, got = time.monotonic(), b""
    while time.monotonic() - t1 < 12:
        try:
            c = s.recv(4096)
        except socket.timeout:
            continue
        if not c:
            print(f"[{time.monotonic()-t1:.2f}s] ← 对端关闭")
            break
        print(f"[{time.monotonic()-t1:.2f}s] ← 收到 {len(c)}B: {c.hex()[:200]}")
        got += c
    else:
        print("12s 内没有关闭、也没有新数据 → 连接保持")
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

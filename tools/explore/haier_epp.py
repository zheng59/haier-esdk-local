#!/usr/bin/env python3
"""解析 56800 上推来的明文 EPP 帧（只读）。

假设（待验证）：
  外层 = [头部: 含设备ID] + [1字节 EPP 总长] + [EPP 帧] + [1字节 外层校验]
  EPP  = ff ff | L = 6 + 1 + len(data) | addr(6) | type(1) | data | checksum
  checksum = sum(L + type + data) & 0xFF

用法：python3 haier_epp.py 192.168.1.100
"""
import socket
import struct
import sys
import time


def capture(host: str, port: int = 56800, wait: float = 2.5) -> bytes:
    s = socket.create_connection((host, port), timeout=wait)
    try:
        s.settimeout(0.5)
        buf, t0 = b"", time.monotonic()
        while time.monotonic() - t0 < wait:
            try:
                c = s.recv(4096)
                if not c:
                    break
                buf += c
            except socket.timeout:
                continue
        return buf
    finally:
        s.close()


def epp_checksum_ok(seg: bytes, l_at: int) -> bool:
    """seg 从 L 字节开始；checksum 是最后一个字节。"""
    return (sum(seg) & 0xFF) == 0


def parse_epp(seg: bytes) -> dict | None:
    """seg 以 ff ff 开头。返回解析结果或 None。"""
    if seg[:2] != b"\xff\xff" or len(seg) < 4:
        return None
    L = seg[2]
    total = 3 + L
    if len(seg) < total:
        return None
    body = seg[3:total]
    data = body[7:-1] if len(body) >= 8 else b""
    return {
        "total": total,
        "L": L,
        "addr": body[:6].hex(),
        "type": f"0x{body[6]:02x}",
        "data": data.hex(),
        "checksum": f"0x{body[-1]:02x}",
        "checksum_ok": sum(seg[2:total]) & 0xFF == 0,
        "raw": seg[:total].hex(),
    }


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
    buf = capture(host)
    print(f"收到 {len(buf)} 字节\n")

    # 1) 外层切分：拿「1 字节长度 + ff ff」找边界
    print("① 找 EPP 起点（模式：<len> ff ff ...）")
    starts = []
    for i in range(len(buf) - 3):
        if buf[i + 1:i + 3] == b"\xff\xff":
            starts.append((i, buf[i]))
    for off, L in starts:
        print(f"   偏移 0x{off:03x}: 外层长度字节=0x{L:02x} ({L})，后面跟 ff ff")

    print("\n② 逐帧解析（EPP：ff ff | L | addr6 | type | data | checksum）")
    for off, L in starts:
        frame = parse_epp(buf[off + 1:])
        if not frame:
            print(f"   0x{off:03x}: 解析失败")
            continue
        print(f"   0x{off:03x}: L={frame['L']} type={frame['type']} "
              f"checksum={frame['checksum']} ok={frame['checksum_ok']}")
        print(f"        addr={frame['addr']}")
        print(f"        data({len(bytes.fromhex(frame['data']))}) = {frame['data']}")
        print(f"        尾部外层字节 = 0x{buf[off + 1 + frame['total']]:02x}")

    # 3) 外层头里有哪些文本
    print("\n③ 外层头文本片段")
    for off, L in starts:
        head = buf[:off]
        runs, cur = [], bytearray()
        for b in head:
            if 32 <= b < 127:
                cur.append(b)
            else:
                if len(cur) >= 4:
                    runs.append(cur.decode())
                cur = bytearray()
        if len(cur) >= 4:
            runs.append(cur.decode())
        print(f"   0x{off:03x} 之前({len(head)}B): {runs}")
        break
    return 0


if __name__ == "__main__":
    sys.exit(main())

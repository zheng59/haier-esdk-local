#!/usr/bin/env python3
"""把 56800 推送的外层封包包头逐字节对齐分析（只读）。"""
import socket
import sys
import time


def capture(host: str, port: int = 56800, wait: float = 2.0) -> bytes:
    s = socket.create_connection((host, port), timeout=wait)
    try:
        s.settimeout(0.4)
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


host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
buf = capture(host)
print(f"总长 {len(buf)} 字节\n")

# 找所有 ff ff
starts = [i for i in range(len(buf) - 3) if buf[i + 1:i + 3] == b"\xff\xff" and buf[i] > 4]
print("EPP 长度字节位置:", [hex(i) for i in starts])

# 消息边界 = 上一个消息结束
msgs = []
prev_end = 0
for i, off in enumerate(starts):
    L = buf[off]
    end = off + 1 + 3 + L - 3  # off+1 是 ff，帧总长 3+L，消息结束 = off+1+3+L
    end = off + 1 + (3 + L)
    if i == len(starts) - 1:
        end = len(buf)
    msgs.append((prev_end, end))
    prev_end = end
print("消息边界:", [(hex(a), hex(b), b - a) for a, b in msgs])

for idx, (a, b) in enumerate(msgs):
    m = buf[a:b]
    print(f"\n=== 消息 {idx}: 长 {len(m)} 字节 (0x{a:x}..0x{b:x}) ===")
    # 找设备 ID (ASCII 12 hex)
    devpos = m.find(b"001122334455")
    print(f"  设备ID ASCII 出现在偏移 {hex(devpos) if devpos >= 0 else '未找到'}")
    if devpos >= 0:
        # 设备 ID 字段按 32 字节算：找它后面第一个非零
        field_end = devpos
        while field_end < len(m) and m[field_end] != 0:
            field_end += 1
        print(f"  设备ID 字符结束于 {hex(field_end)}，后面填充 "
              f"{m[field_end:field_end+8].hex()}")
    # 打印头 16 字节
    print(f"  头 16 字节: {m[:16].hex()}")
    # 第一个 EPP 帧位置
    epp = m.find(b"\xff\xff")
    print(f"  ff ff 出现在 {hex(epp)}")
    # 打印 EPP 前面的 12 字节
    print(f"  EPP 前 12 字节: {m[max(0,epp-12):epp].hex()}")
    print(f"  EPP 后到结尾: {m[epp:epp+12].hex()} … 尾 4 字节 = {m[-4:].hex()}")

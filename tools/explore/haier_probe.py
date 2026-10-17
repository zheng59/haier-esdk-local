#!/usr/bin/env python3
"""
海尔 U+ / uHome 局域网探测器（只读，不改动设备）

用途：判断一台「能用海尔智家 / Haismart App 控制」的海尔空调，
      是否在局域网上讲 uSS / UDISCOVERY 协议（也就是有没有纯本地控制的可能）。

协议参考：enapt/haismart-local 的 PROTOCOL.md（独立逆向，非官方文档）
  - UDP 7083 : UDISCOVERY，无需密钥、无需账号，一个来回返回设备信息
  - TCP 56800: uSS 控制协议（握手明文，业务数据 AES-128-CBC 加密）

用法：
  python3 haier_probe.py 192.168.1.23      # 单播探测（最可靠，推荐）
  python3 haier_probe.py --broadcast       # 广播探测（源端口会绑定 7083）
  python3 haier_probe.py --selftest        # 自检：用伪造回包验证解析逻辑

退出码：0 = 探测到本地协议；1 = 没探测到；2 = 参数/环境错误
"""

import argparse
import socket
import struct
import sys

MAGIC = b"Haier"
CMD_SEARCH = 0x6915      # 请求：搜索
CMD_DEVINFO = 0x684D     # 应答：设备信息
PORT_UDISCOVERY = 7083
PORT_USS = 56800

# UDISCOVERY 报文头共 21 字节（0x15）
HDR_MAGIC = 0x00
HDR_CMD = 0x05
HDR_FLAGS = 0x09
HDR_ZEROS = 0x0B
HDR_PAYLOAD_LEN = 0x11
HDR_END = 0x15

# 应答体 0x684d（参考家族 309 字节）
R_DEVID = 0x15
R_UPLUSID = 0x25
R_TLV_COUNT = 0x45
R_TLV_AREA = 0x49
R_IP = 0xE5
R_PORT = 0xF5
R_SDK = 0xFD
R_FW = 0x105
R_TAG = 0x115

CLOUD_STATES = {
    1000: "已连接（能连上海尔云）",
    1010: "连接中断，模块重试中",
    1006: "已断开（被切断/离线）",
}

# ---- uSS（TCP 56800）明文握手：不需要 localKey ----
INFO_HELLO = 0
INFO_HELLO_RESP = 1
INFO_HELLO_DONE = 2
INFO_HELLO_DONE_RESP = 3
TYPE_BYTE = {2: 0x01, 3: 0x6E}   # pro_ver -> 头里第 7 个字节


def uss_encode(info_type: int, sn: int, payload: bytes = b"",
               type_byte: int = 0x01, flag: int = 0, session: int = 0) -> bytes:
    return struct.pack(">IHBBIHH", 0xEA60 + info_type, len(payload) + 0x0A,
                       type_byte & 0xFF, flag & 0xFF, sn & 0xFFFFFFFF, 0, session & 0xFFFF) + payload


def uss_decode(buf: bytes) -> dict:
    info_code, length, type_byte, flag, sn, code2, session = struct.unpack(">IHBBIHH", buf[:16])
    end = 6 + length
    return {
        "info_type": info_code - 0xEA60,
        "info_code": f"0x{info_code:04x}",
        "type_byte": f"0x{type_byte:02x}",
        "flag": flag,
        "sn": sn,
        "code2": code2,
        "session": session,
        "payload": buf[16:end] if end <= len(buf) else buf[16:],
        "raw": buf[:end] if end <= len(buf) else buf,
    }


def uss_recv(sock: socket.socket, timeout: float) -> dict | None:
    """收一条完整的 uSS 消息（头 16 字节，总长 = 6 + 长度字段）。"""
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < 16:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buf += chunk
    total = 6 + struct.unpack(">H", buf[4:6])[0]
    while len(buf) < total:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return uss_decode(buf)


def uss_handshake(host: str, device_id: str, pro_ver: int = 2,
                  timeout: float = 4.0) -> dict:
    """只走握手，不发任何 biz-data（即不读、不写空调状态）。"""
    dev = device_id.encode("ascii").ljust(32, b"\x00")[:32]
    out: dict = {"pro_ver": pro_ver, "device_id": device_id}
    s = socket.create_connection((host, PORT_USS), timeout=timeout)
    try:
        s.sendall(uss_encode(INFO_HELLO, 1, dev, type_byte=TYPE_BYTE[pro_ver]))
        resp = uss_recv(s, timeout)
        if resp is None:
            out["error"] = "没有收到 HELLO_RESP（连接被关）"
            return out
        out["hello_resp"] = {k: v for k, v in resp.items() if k not in ("raw", "payload")}
        out["hello_resp_payload_hex"] = resp["payload"][:32].hex()
        if resp["info_type"] != INFO_HELLO_RESP:
            out["error"] = f"应答不是 HELLO_RESP（{resp['info_code']}）"
            return out
        if len(resp["payload"]) >= 8:
            status, ver = struct.unpack(">II", resp["payload"][:8])
            out["status"] = status
            out["localkey_version"] = ver
        # 用设备自己的版本字节继续（它比对版本不符会静默丢包）
        speak = int(resp["type_byte"], 16) or TYPE_BYTE[pro_ver]
        s.sendall(uss_encode(INFO_HELLO_DONE, 2, type_byte=speak, session=resp["session"]))
        done = uss_recv(s, timeout)
        if done is not None:
            out["hello_done_resp"] = {k: v for k, v in done.items() if k not in ("raw", "payload")}
            out["hello_done_flag"] = done["flag"]
            out["hello_done_payload_hex"] = done["payload"][:64].hex()
            out["hello_done_payload_len"] = len(done["payload"])
    finally:
        s.close()
    return out


def build_request() -> bytes:
    """构造 56 字节 payload + 21 字节头的搜索请求。"""
    payload = bytearray(56)
    # +0x10 必须是字面量 "2.0.0"，+0x18 必须是字面量 "UDISCOVERY_SDK"，否则设备不回话
    payload[0x10:0x15] = b"2.0.0"
    payload[0x18:0x18 + len(b"UDISCOVERY_SDK")] = b"UDISCOVERY_SDK"
    header = bytearray()
    header += MAGIC
    header += struct.pack(">I", CMD_SEARCH)
    header += struct.pack(">H", 0x020A)
    header += b"\x00" * 6
    header += struct.pack(">I", len(payload))
    assert len(header) == HDR_END
    return bytes(header) + bytes(payload)


def _ascii(data: bytes) -> str:
    return data.rstrip(b"\x00").decode("ascii", "replace")


def parse_reply(data: bytes) -> dict:
    """解析 0x684d 应答。按字段偏移读，TLV 区按记录走。"""
    if len(data) < HDR_END:
        raise ValueError(f"包太短：{len(data)} 字节")

    cmd = struct.unpack(">I", data[HDR_CMD:HDR_CMD + 4])[0]
    info = {"raw_len": len(data), "cmd": f"0x{cmd:04x}"}
    if cmd != CMD_DEVINFO:
        info["warning"] = f"命令字不是 0x684d（{info['cmd']}）"

    info["device_id"] = _ascii(data[R_DEVID:R_DEVID + 16])

    uplus_raw = data[R_UPLUSID:R_UPLUSID + 32]
    info["uplus_id_hex"] = uplus_raw.hex()
    # BCD 打包，hex 即云端设备列表里的 wifiType；去掉尾部填充的 0
    info["uplus_id"] = uplus_raw.hex().rstrip("0") or "(空)"

    info["ip"] = _ascii(data[R_IP:R_IP + 16])
    if len(data) >= R_PORT + 2:
        info["port"] = struct.unpack(">H", data[R_PORT:R_PORT + 2])[0]
    info["sdk"] = _ascii(data[R_SDK:R_SDK + 5])
    info["firmware"] = [data[R_FW + i:R_FW + i + 8].hex() for i in (0, 8)]
    info["proto_tag"] = _ascii(data[R_TAG:R_TAG + 32])

    # TLV 区：type(1) | length(1) | value
    tlvs = []
    off = R_TLV_AREA
    count_hint = struct.unpack(">I", data[R_TLV_COUNT:R_TLV_COUNT + 4])[0]
    info["tlv_count_field"] = count_hint
    for _ in range(32):
        if off + 2 > len(data):
            break
        t = data[off]
        if t == 0:
            break
        ln = data[off + 1]
        val = data[off + 2:off + 2 + ln]
        tlvs.append((t, ln, val))
        off += 2 + ln
    info["tlvs"] = tlvs

    for t, ln, val in tlvs:
        if t == 0x01:
            info["tlv_device_id"] = _ascii(val)
        elif t == 0x03:
            code = int.from_bytes(val, "big")
            info["cloud_state_code"] = code
            info["cloud_state"] = CLOUD_STATES.get(code, f"未知代码 {code}（请反馈）")
    return info


def _selftest_reply() -> bytes:
    """伪造一个 0x684d 应答，用来验证解析逻辑。"""
    buf = bytearray(309)
    buf[HDR_MAGIC:HDR_MAGIC + 5] = MAGIC
    buf[HDR_CMD:HDR_CMD + 4] = struct.pack(">I", CMD_DEVINFO)
    buf[R_DEVID:R_DEVID + 16] = b"A4C138112233".ljust(16, b"\x00")
    buf[R_UPLUSID:R_UPLUSID + 32] = bytes.fromhex("02 01 20 36".replace(" ", "")).ljust(32, b"\x00")
    buf[R_TLV_COUNT:R_TLV_COUNT + 4] = struct.pack(">I", 2)
    off = R_TLV_AREA
    for t, v in ((0x01, b"A4:C1:38:11:22:33"), (0x03, struct.pack(">H", 1000))):
        buf[off] = t
        buf[off + 1] = len(v)
        buf[off + 2:off + 2 + len(v)] = v
        off += 2 + len(v)
    buf[R_IP:R_IP + 16] = b"192.168.1.23".ljust(16, b"\x00")
    buf[R_PORT:R_PORT + 2] = struct.pack(">H", PORT_USS)
    buf[R_SDK:R_SDK + 5] = b"2.0.0"
    buf[R_FW:R_FW + 16] = bytes(range(16))
    buf[R_TAG:R_TAG + 32] = b"UDISCOVERY_UWT".ljust(32, b"\x00")
    return bytes(buf)


def selftest() -> int:
    info = parse_reply(_selftest_reply())
    ok = (
        info["device_id"] == "A4C138112233"
        and info["uplus_id"] == "02012036"
        and info["ip"] == "192.168.1.23"
        and info["port"] == PORT_USS
        and info.get("cloud_state_code") == 1000
    )
    for k, v in info.items():
        if k != "tlvs":
            print(f"  {k:18} = {v}")

    # uSS 编解码往返
    raw = uss_encode(INFO_HELLO_RESP, 1, struct.pack(">II", 1, 4), session=0x1234)
    msg = uss_decode(raw)
    uss_ok = (msg["info_type"] == INFO_HELLO_RESP and msg["session"] == 0x1234
              and struct.unpack(">II", msg["payload"]) == (1, 4) and msg["raw"] == raw)
    print(f"  uss_roundtrip      = {'ok' if uss_ok else 'FAIL'}  payload={msg['payload'].hex()}")

    ok = ok and uss_ok
    print("自检：", "通过 ✅" if ok else "失败 ❌")
    return 0 if ok else 1


def probe_udp(target: str | None, timeout: float, bind_7083: bool) -> bytes | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        # 广播必须从源端口 7083 发出，否则设备静默丢弃
        if bind_7083:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", PORT_UDISCOVERY))
        s.settimeout(timeout)
        dest = target or "255.255.255.255"
        s.sendto(build_request(), (dest, PORT_UDISCOVERY))
        while True:
            data, addr = s.recvfrom(4096)
            return data, addr
    except socket.timeout:
        return None
    except OSError as e:
        print(f"  UDP 发送失败：{e}")
        return None
    finally:
        s.close()


def probe_tcp(host: str, timeout: float = 2.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, PORT_USS)) == 0
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="海尔 U+ 局域网探测器（只读）")
    ap.add_argument("host", nargs="?", help="空调的局域网 IP；省略则用广播")
    ap.add_argument("--broadcast", action="store_true", help="强制广播探测")
    ap.add_argument("--timeout", type=float, default=3.0, help="等待秒数（默认 3）")
    ap.add_argument("--device-id", help="跳过发现，直接用这个设备 ID 做握手")
    ap.add_argument("--pro-ver", type=int, choices=(2, 3), default=2, help="uSS 协议版本（默认 2）")
    ap.add_argument("--dump", action="store_true", help="打印发现应答的原始 hex")
    ap.add_argument("--selftest", action="store_true", help="只跑解析自检")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    info: dict = {}
    if args.device_id:
        target, got = args.host, True
    else:
        target = None if args.broadcast else args.host
        print(f"① UDP {PORT_UDISCOVERY} 探测 {'广播' if not target else target} …")
        got = probe_udp(target, args.timeout, bind_7083=(target is None))
        if not got:
            print("  ❌ 没有回应")
            print("  → 可能原因：模块不认识 UDISCOVERY / 不在同一网段 / AP 过滤了广播")
            print("    先确认空调 IP，再用单播重试；或直接给 --device-id 走握手。")
            return 1

        data, addr = got
        print(f"  ✅ 来自 {addr[0]}:{addr[1]}，{len(data)} 字节")
        if args.dump:
            print(f"    raw = {data.hex()}")
        try:
            info = parse_reply(data)
        except ValueError as e:
            print(f"  解析失败：{e}")
            return 1
        for k, v in info.items():
            if k == "tlvs":
                v = [f"type=0x{t:02x} len={ln} value={val.hex()}" for t, ln, val in v]
            print(f"    {k:18} = {v}")

    known_ip = args.host or info.get("ip")
    device_id = args.device_id or info.get("tlv_device_id") or info.get("device_id")
    if not device_id:
        print("拿不到 device_id，无法握手。用 --device-id 指定（通常就是 Wi-Fi 模块的 MAC 去冒号）")
        return 1

    print(f"② TCP {PORT_USS} 连接 {known_ip} …")
    if not probe_tcp(known_ip):
        print("  ❌ 56800 不通 → 本地协议这条路大概率走不通")
        return 1
    print("  ✅ 本地控制端口开着")

    print(f"③ uSS 明文握手（device_id={device_id}, pro_ver={args.pro_ver}）…")
    hs = uss_handshake(known_ip, device_id, pro_ver=args.pro_ver, timeout=args.timeout + 1)
    for k, v in hs.items():
        print(f"    {k:22} = {v}")
    if hs.get("error"):
        print("\n  握手失败。换 --pro-ver 3 再试一次；若都不行，说明这代固件不认标准 uSS 握手。")
        return 1
    print("\n  ✅ 握手成功 → 这台设备就是标准 uSS，**只差 localKey 就能全本地读写了**")
    return 0


if __name__ == "__main__":
    sys.exit(main())

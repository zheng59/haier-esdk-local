"""客户端逻辑单元测试（用假 socket，不碰真机）。"""
from __future__ import annotations

import pathlib
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import component_loader  # noqa: F401,E402

from haier_esdk.client import (ADDR_MODULE, HaierClient, HaierCommandRejected,  # noqa: E402
                              HaierError)
from haier_esdk.protocol import (ADDR_AC, CMD_GET_STATUS, CMD_POWER_OFF, CMD_POWER_ON,  # noqa: E402
                                 CMD_SET, MAGIC_REQUEST, MAGIC_RESPONSE, TYPE_ALARM,
                                 TYPE_ALARM_RESPONSE, TYPE_GET_ALARM, TYPE_INVALID,
                                 TYPE_REPORT, TYPE_STATUS, build_request, epp_frame,
                                 parse_epp)

DEVICE_ID = b"001122334455"
BLOCK = bytes.fromhex("001b003a000300000000000100020000000900000000000b")
BURST = bytes.fromhex(
    "00002715" + "00" * 36 + DEVICE_ID.hex() + "00" * 27 + "13"
    "ffff10000000000001040f5a0000000000007e"
    "00002715" + "00" * 36 + DEVICE_ID.hex() + "00" * 27 + "25"
    "ffff22000000000001066d01001b003a000300000000000100020000000900000000000b06")


def envelope(frame: bytes, seq: int, magic: bytes = MAGIC_RESPONSE) -> bytes:
    return (magic + b"\x00" * 36 + DEVICE_ID + b"\x00" * 20
            + seq.to_bytes(4, "big") + len(frame).to_bytes(4, "big") + frame)


def _set_bit(block: bytes, index: int, mask: int, on: bool) -> bytes:
    value = block[index] | mask if on else block[index] & ~mask
    return block[:index] + bytes([value]) + block[index + 1:]


class FakeServer:
    """按请求回一条合理应答，记录收到的请求。"""

    def __init__(self, block: bytes = BLOCK, silent: bool = False,
                 invalid: set[int] | None = None, fault: bytes = b"\x00" * 6) -> None:
        self.block = bytes(block)
        self.silent = silent
        self.invalid = invalid or set()
        self.fault = fault
        self.requests: list[bytes] = []

    def initial(self) -> bytes:
        """连上就推两条：故障位图 + 状态（和真机一样）。"""
        alarm = envelope(epp_frame(b"\x0f\x5a" + self.fault, frame_type=TYPE_ALARM,
                                  addr=ADDR_MODULE), 0)
        report = envelope(epp_frame(b"\x6d\x01" + self.block, frame_type=TYPE_REPORT,
                                    addr=ADDR_MODULE), 0)
        return alarm + report

    def handle(self, request: bytes) -> bytes:
        self.requests.append(request)
        assert request[:4] == MAGIC_REQUEST, "客户端必须用请求魔数 2714"
        seq = int.from_bytes(request[72:76], "big")
        length = int.from_bytes(request[76:80], "big")
        frame = parse_epp(request[80:80 + length])
        assert frame is not None and frame.checksum_ok
        if self.silent:
            return b""
        sub = frame.subcommand
        if frame.frame_type == TYPE_GET_ALARM:
            # 0x74 的载荷同样是「命令字 + 位图」
            return envelope(epp_frame(b"\x0f\x5a" + self.fault, frame_type=TYPE_ALARM_RESPONSE,
                                      addr=ADDR_MODULE), seq)
        if sub in self.invalid:
            return envelope(epp_frame(b"\x00\x00", frame_type=TYPE_INVALID,
                                      addr=ADDR_MODULE), seq)
        if sub == CMD_POWER_OFF:
            self.block = _set_bit(self.block, 17, 0x01, False)
        elif sub == CMD_POWER_ON:
            self.block = _set_bit(self.block, 17, 0x01, True)
        elif sub == CMD_SET:
            self.block = bytes(frame.data[2:26])       # 主板原样接受
        return envelope(epp_frame(b"\x6d\x01" + self.block, frame_type=TYPE_STATUS,
                                  addr=ADDR_MODULE), seq)


class FakeSocket:
    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self._in = bytearray(server.initial())
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, value: float) -> None:      # noqa: D102
        self.timeout = value

    def recv(self, bufsize: int) -> bytes:           # noqa: D102
        if not self._in:
            raise socket.timeout()
        chunk = bytes(self._in[:bufsize])
        del self._in[:bufsize]
        return chunk

    def sendall(self, data: bytes) -> None:          # noqa: D102
        self.sent.append(data)
        self._in += self.server.handle(data)

    def close(self) -> None:                         # noqa: D102
        self.closed = True


class ClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeServer()
        self.sockets: list[FakeSocket] = []

        def _factory(*args: object, **kwargs: object) -> FakeSocket:
            sock = FakeSocket(self.server)
            self.sockets.append(sock)
            return sock

        patcher = mock.patch("socket.create_connection", side_effect=_factory)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.client = HaierClient("192.0.2.10")
        self.client.connect()
        self.socket = self.sockets[0]
        self.addCleanup(self.client.close)

    def test_connect_gives_device_id_and_burst(self) -> None:
        self.assertEqual(self.client.device_id, DEVICE_ID.decode())
        self.assertEqual(self.server.requests, [])          # 只收，没发
        self.assertEqual([e.frame.frame_type for e in self.client.burst],
                         [TYPE_ALARM, TYPE_REPORT])

    def test_status_from_burst_needs_no_request(self) -> None:
        status = self.client.status_from_burst()
        assert status is not None
        self.assertEqual(status.raw, BLOCK)
        self.assertEqual(self.server.requests, [])

    def test_read_status(self) -> None:
        status = self.client.read_status()
        self.assertTrue(status.power)
        self.assertEqual((status.mode, status.fan, status.target_temperature), ("cool", "low", 27.0))
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(parse_epp(self.server.requests[0][80:93]).subcommand, CMD_GET_STATUS)

    def test_sequence_increments(self) -> None:
        self.client.read_status()
        self.client.read_status()
        seqs = [int.from_bytes(r[72:76], "big") for r in self.server.requests]
        self.assertEqual(seqs, [1, 2])

    def test_power_off_uses_dedicated_command(self) -> None:
        status = self.client.set_hvac(power=False)
        self.assertFalse(status.power)
        self.assertEqual(len(self.server.requests), 1)
        frame = parse_epp(self.server.requests[0][80:])
        self.assertEqual(frame.subcommand, CMD_POWER_OFF)

    def test_set_temperature_refills_block(self) -> None:
        self.client.set_hvac(temperature=25)
        self.assertEqual(len(self.server.requests), 1)
        frame = parse_epp(self.server.requests[0][80:])
        assert frame is not None
        self.assertEqual(frame.subcommand, CMD_SET)
        block = frame.data[2:26]
        self.assertEqual(block[23], 25 - 16)
        self.assertEqual(block[5], 0)                       # cntrl 归零
        self.assertEqual(block[:5], BLOCK[:5])              # 其它字段不动
        self.assertEqual(self.server.block, block)

    def test_power_on_and_mode_in_one_session(self) -> None:
        """关机状态下改模式：先 4d02 开机，再 4d5f 设模式，全程一个连接、不带多余的读。"""
        self.client.close()
        self.server.block = _set_bit(BLOCK, 17, 0x01, False)
        self.client = HaierClient("192.0.2.10")
        self.client.connect()          # 新连接的推送里就是「关机」态
        status = self.client.set_hvac(power=True, mode="heat")
        self.assertEqual([parse_epp(r[80:]).subcommand for r in self.server.requests],
                         [CMD_POWER_ON, CMD_SET])
        self.assertEqual(status.mode, "heat")
        self.assertTrue(status.power)
        # 全程只用了一个新连接（先开关机再回填，不开第二个会话）
        self.assertEqual(len(self.sockets), 2)
        self.assertFalse(self.sockets[-1].closed)

    def test_noop_write_sends_nothing(self) -> None:
        status = self.client.set_hvac(mode="cool")          # 已经就是 cool
        self.assertEqual(self.server.requests, [])
        self.assertEqual(status.raw, BLOCK)

    def test_health_and_swing(self) -> None:
        self.client.set_hvac(health=False, swing=True)
        block = parse_epp(self.server.requests[0][80:]).data[2:26]
        self.assertEqual(block[17] & 0x08, 0)
        self.assertEqual(block[15], 1)

    def test_swing_write_clears_c19_gate(self) -> None:
        """真机上 c19 bit0 被 App 置上后 c15 就失效了 → 写摆风时必须把 bit0 清掉。"""
        self.client.close()
        self.server.block = bytes(BLOCK[:19] + b"\x01" + BLOCK[20:])   # c19 bit0 = 1
        self.client = HaierClient("192.0.2.10")
        self.client.connect()
        self.client.set_hvac(swing=True)
        block = parse_epp(self.server.requests[0][80:]).data[2:26]
        self.assertEqual(block[15], 1)
        self.assertEqual(block[19] & 0x01, 0)

    def test_invalid_answer_raises(self) -> None:
        self.server.invalid = {CMD_POWER_OFF}
        with self.assertRaises(HaierCommandRejected):
            self.client.set_hvac(power=False)

    def test_read_fault(self) -> None:
        self.assertEqual(self.client.read_fault(), b"\x00" * 6)
        frame = parse_epp(self.server.requests[0][80:])
        assert frame is not None
        self.assertEqual(frame.frame_type, TYPE_GET_ALARM)

    def test_request_timeout_raises(self) -> None:
        self.server.silent = True
        with self.assertRaises(HaierError):
            self.client.request(epp_frame(b"\x4d\x01", addr=ADDR_AC), wait=0.05, retries=0)

    def test_peer_close_raises(self) -> None:
        self.client._sock._in = bytearray()                 # 之后 recv 会超时
        self.client._sock.recv = lambda n: b""              # 模拟对端关闭
        with self.assertRaises(HaierError):
            self.client.read_status()

    def test_request_magic_is_used(self) -> None:
        req = build_request(epp_frame(b"\x4d\x01", addr=ADDR_AC), seq=3)
        self.assertEqual(req[:4], MAGIC_REQUEST)


if __name__ == "__main__":
    unittest.main()

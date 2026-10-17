"""协议层单元测试（离线，不碰真机）。

跑：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import component_loader  # noqa: F401,E402  导入即加载集成里的 protocol/client

from haier_esdk.protocol import (ADDR_AC, ADDR_MODULE, MAGIC_REQUEST, MAGIC_RESPONSE,  # noqa: E402
                                 TYPE_ALARM, TYPE_REPORT, TYPE_STATUS, HaierStatus,
                                 build_request, epp_frame, parse_envelopes, parse_epp)

DEVICE_ID = b"001122334455"

# 2026-10-17 真机抓到的「连上即推」216 字节（两条消息）
BURST = bytes.fromhex(
    "00002715" + "00" * 36 + DEVICE_ID.hex() + "00" * 27 + "13"
    "ffff10000000000001040f5a0000000000007e"
    "00002715" + "00" * 36 + DEVICE_ID.hex() + "00" * 27 + "25"
    "ffff22000000000001066d01001b003a000300000000000100020000000900000000000b06")
BLOCK = bytes.fromhex("001b003a000300000000000100020000000900000000000b")


class TestEpp(unittest.TestCase):
    def test_checksum_is_plain_sum(self) -> None:
        """校验和 = (L + addr + type + data) & 0xFF，不是补码。"""
        self.assertEqual(epp_frame(bytes.fromhex("4d01"), addr=ADDR_AC).hex(),
                         "ffff0a000000000000014d0159")
        self.assertEqual(epp_frame(bytes.fromhex("4d02"), addr=ADDR_AC).hex(),
                         "ffff0a000000000000014d025a")
        self.assertEqual(epp_frame(bytes.fromhex("4d03"), addr=ADDR_AC).hex(),
                         "ffff0a000000000000014d035b")

    def test_length_counts_checksum(self) -> None:
        frame = epp_frame(bytes.fromhex("4d01"), addr=ADDR_AC)
        self.assertEqual(frame[2], 6 + 1 + 2 + 1)
        self.assertEqual(len(frame), 3 + frame[2])

    def test_parse_round_trip(self) -> None:
        frame = epp_frame(bytes.fromhex("4d5f") + BLOCK, addr=ADDR_AC)
        parsed = parse_epp(frame)
        assert parsed is not None
        self.assertTrue(parsed.checksum_ok)
        self.assertEqual(parsed.subcommand, 0x4D5F)
        self.assertEqual(parsed.data[2:], BLOCK)

    def test_bad_checksum_detected(self) -> None:
        frame = bytearray(epp_frame(bytes.fromhex("4d01"), addr=ADDR_AC))
        frame[-1] ^= 0xFF
        parsed = parse_epp(bytes(frame))
        assert parsed is not None
        self.assertFalse(parsed.checksum_ok)


class TestEnvelope(unittest.TestCase):
    def test_request_layout(self) -> None:
        """80 字节头：magic + 36×0 + MAC(12) + 20×0 + 序号(BE32) + 帧长(BE32)。"""
        frame = epp_frame(bytes.fromhex("4d01"), addr=ADDR_AC)
        req = build_request(frame, seq=7, mac=DEVICE_ID)
        self.assertEqual(len(req), 80 + len(frame))
        self.assertEqual(req[:4], MAGIC_REQUEST)
        self.assertEqual(req[40:52], DEVICE_ID)
        self.assertEqual(int.from_bytes(req[72:76], "big"), 7)
        self.assertEqual(int.from_bytes(req[76:80], "big"), len(frame))
        self.assertEqual(req[80:], frame)

    def test_request_magic_differs_from_response(self) -> None:
        """请求方向 2714、响应方向 2715 —— 用错方向模块不会转发给主板。"""
        self.assertNotEqual(MAGIC_REQUEST, MAGIC_RESPONSE)
        self.assertEqual(MAGIC_RESPONSE, BURST[:4])

    def test_parse_burst(self) -> None:
        envelopes, rest = parse_envelopes(BURST)
        self.assertEqual(rest, b"")
        self.assertEqual(len(envelopes), 2)
        self.assertEqual([e.frame.frame_type for e in envelopes], [TYPE_ALARM, TYPE_REPORT])
        self.assertEqual([e.frame.subcommand for e in envelopes], [0x0F5A, 0x6D01])
        self.assertTrue(all(e.frame.checksum_ok for e in envelopes))
        self.assertEqual(envelopes[0].raw_envelope[40:52], DEVICE_ID)

    def test_partial_stream_is_kept(self) -> None:
        """半条消息要留着，等下一批字节来了再拼。"""
        head, tail = BURST[:100], BURST[100:]
        envelopes, rest = parse_envelopes(head)
        self.assertEqual(len(envelopes), 1)          # 第一条完整
        envelopes, rest = parse_envelopes(rest + tail)
        self.assertEqual(len(envelopes), 1)          # 补齐后第二条也出来
        self.assertEqual(rest, b"")

    def test_garbage_between_messages_is_skipped(self) -> None:
        envelopes, _ = parse_envelopes(b"\x11\x22" + BURST)
        self.assertEqual(len(envelopes), 2)


class TestStatus(unittest.TestCase):
    def setUp(self) -> None:
        self.status = HaierStatus(BLOCK)

    def test_decode(self) -> None:
        self.assertTrue(self.status.power)
        self.assertEqual(self.status.room_temperature, 27.0)
        self.assertEqual(self.status.room_humidity, 58)
        self.assertEqual(self.status.target_temperature, 27.0)
        self.assertEqual(self.status.mode, "cool")
        self.assertEqual(self.status.fan, "low")
        self.assertTrue(self.status.health)
        self.assertFalse(self.status.display_off)
        self.assertEqual(self.status.swing, 0)
        self.assertEqual(self.status.preset, "none")

    def test_mutations_keep_block_size(self) -> None:
        for mutated in (self.status.with_power(False), self.status.with_target(22),
                        self.status.with_mode("heat"), self.status.with_fan("auto"),
                        self.status.with_health(False), self.status.with_byte(15, 1)):
            self.assertEqual(len(mutated.raw), 24)

    def test_with_target_sets_whole_and_half_bits(self) -> None:
        self.assertEqual(self.status.with_target(22).raw[23], 22 - 16)
        self.assertFalse(self.status.with_target(22).raw[17] & 0x20)
        half = self.status.with_target(26.5)
        self.assertEqual(half.raw[23], 10)
        self.assertTrue(half.raw[17] & 0x20)

    def test_with_target_range_checked(self) -> None:
        with self.assertRaises(ValueError):
            self.status.with_target(31)
        with self.assertRaises(ValueError):
            self.status.with_target(15)

    def test_for_sending_zeroes_cntrl(self) -> None:
        """esp-home 同款做法：回填时 cntrl 字节（c5）必须归 0。"""
        self.assertNotEqual(self.status.raw[5], 0)
        self.assertEqual(self.status.for_sending()[5], 0)
        self.assertEqual(len(self.status.for_sending()), 24)

    def test_wrong_block_size_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HaierStatus(b"\x00" * 23)

    def test_from_frame_uses_data_after_subcommand(self) -> None:
        envelopes, _ = parse_envelopes(BURST)
        self.assertEqual(HaierStatus.from_frame(envelopes[1].frame).raw, BLOCK)


if __name__ == "__main__":
    unittest.main()

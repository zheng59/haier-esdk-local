#!/usr/bin/env python3
"""字段级命令探测：那 10 个「回 STATUS 却动了设置」的命令字，各管什么？

背景（2026-10-17 全扫 4d00~4dff 时发现）：
  4d04 / 4d08 / 4d09 / 4d18 / 4d19 / 4d1e / 4d1f / 4d22 / 4d23 / 4d24
这些字回的是 STATUS（不是 INVALID），但状态块跟着变了（c16 遥控锁被置上、c17 健康位翻转、c19 置位）。
怀疑它们是「字段级 set」而不是纯读 —— 如果真是，那主板经 `4d5f` 忽略的那几项
（**显示屏开关 / 极速 / 静音 / 半度**）也许能从这条小路打开。

做法：一次只动一个字，动完立刻整块还原
  ① 读基线块（4d01）
  ② 发一个字（可带载荷）
  ③ 再读一块，diff（排除 c1 室温 / c3 湿度 / c5 交换计数这类实时值）
  ④ 有变化 → 用 4d5f 把基线块整块写回，并二次确认
  ⑤ 打印：字 → 动了哪些字节 → 是否已还原

危险提示：有些字会把空调关机 / 锁遥控器，脚本会在下一步立刻还原，但**别在半夜无人时跑**。

用法：
  python3 haier_field_probe.py 192.168.1.100                     # 默认那 10 个字，空载荷
  python3 haier_field_probe.py 192.168.1.100 --words 4d08,4d09   # 只试指定的几个
  python3 haier_field_probe.py 192.168.1.100 --word 4d08 --payload 01   # 带载荷单试
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from haier_energy_probe import VOLATILE_IDX, Probe, ask_safe      # noqa: E402
from haier_protocol import ADDR_AC, HaierStatus, epp_frame        # noqa: E402

# 嫌疑名单：全扫时"回了 STATUS 却不是 4d01"的字
SUSPECTS = [0x4D04, 0x4D08, 0x4D09, 0x4D18, 0x4D19,
            0x4D1E, 0x4D1F, 0x4D22, 0x4D23, 0x4D24]

# 24 字节块里各位的含义（README §5.4），用来把 diff 翻译成人话
FIELD_NAMES = {
    (11, 0xFF): "模式", (13, 0xFF): "风速", (15, 0xFF): "摆风 c15",
    (16, 0x80): "遥控锁", (16, 0x08): "华氏", (16, 0x20): "十度",
    (17, 0x01): "开关", (17, 0x08): "健康", (17, 0x20): "半度", (17, 0x40): "十度",
    (19, 0x01): "用c19摆风位", (19, 0x02): "极速", (19, 0x04): "静音",
    (19, 0x08): "水平摆风", (19, 0x10): "垂直摆风", (19, 0x20): "显示屏关",
    (23, 0xFF): "设定温度",
}


def explain(index: int, before: int, after: int) -> str:
    """把「某字节 0x?? → 0x??」翻译成涉及的位名。"""
    names = []
    for (idx, mask), label in FIELD_NAMES.items():
        if idx != index:
            continue
        if (before & mask) != (after & mask):
            names.append(f"{label} {'1→0' if before & mask else '0→1'}")
    if not names:
        names.append("位含义未登记")
    return "、".join(names)


def read_block(probe: Probe, wait: float) -> HaierStatus | None:
    envs, _ = ask_safe(probe, epp_frame(b"\x4d\x01", addr=ADDR_AC), wait)
    if not envs:
        return None
    try:
        return HaierStatus.from_frame(envs[0].frame)
    except ValueError:
        return None


def restore(probe: Probe, baseline: bytes, wait: float) -> tuple[bool, str]:
    """把非实时字节改回基线值，整块 4d5f 回填，并二次确认。"""
    current = read_block(probe, wait)
    if current is None:
        return False, "读不回当前块，没法还原"
    fixed = bytearray(current.raw)
    for idx in range(24):
        if idx not in VOLATILE_IDX:
            fixed[idx] = baseline[idx]
    if bytes(fixed) == current.raw:
        return True, "本来就一致"
    try:
        probe.apply(HaierStatus(bytes(fixed)))
    except Exception as exc:                                     # noqa: BLE001
        return False, f"回填失败：{exc}"
    time.sleep(1.0)
    back = read_block(probe, wait)
    if back is None:
        return False, "还原后读不回来"
    same = all(back.raw[i] == baseline[i] for i in range(24) if i not in VOLATILE_IDX)
    return same, ("已还原" if same else f"仍未还原：{back.raw.hex()}")


def try_word(probe: Probe, word: int, payload: bytes, wait: float) -> str | None:
    """试一个字，返回「动了什么」的描述（没动返回 None）。"""
    baseline = read_block(probe, wait)
    if baseline is None:
        print(f"  跳过 {word:04x}：读不到基线块")
        return None
    frame = epp_frame(word.to_bytes(2, "big") + payload, addr=ADDR_AC)
    envs, matched = ask_safe(probe, frame, wait)
    reply = "无应答" if not envs else \
        f"type=0x{envs[0].frame.frame_type:02x} data={envs[0].frame.data.hex()}"
    time.sleep(1.0)
    after = read_block(probe, wait)
    if after is None:
        print(f"  {word:04x}  回执 {reply}｜发完读不回状态块（可能被关机/断连）")
        return "读不回"
    changed = [(i, baseline.raw[i], after.raw[i]) for i in range(24)
               if after.raw[i] != baseline.raw[i] and i not in VOLATILE_IDX]
    if not changed:
        print(f"  {word:04x}  回执 {reply}｜状态块没动")
        return None
    detail = "；".join(f"c{i} 0x{a:02x}→0x{b:02x}（{explain(i, a, b)}）" for i, a, b in changed)
    ok, note = restore(probe, baseline.raw, wait)
    mark = "✓" if ok else "⚠"
    print(f"  {word:04x}  回执 {reply}\n        动了：{detail}\n        {mark} 还原：{note}")
    return detail


def main() -> int:
    ap = argparse.ArgumentParser(description="逐个试「字段级命令」，看它们各改控制块的哪一位")
    ap.add_argument("host", help="空调 IP")
    ap.add_argument("--port", type=int, default=56800)
    ap.add_argument("--words", help="逗号分隔的十六进制命令字（默认 10 个嫌疑字）")
    ap.add_argument("--word", help="只试这一个字")
    ap.add_argument("--payload", default="", help="附加载荷（十六进制，默认空）")
    ap.add_argument("--wait", type=float, default=2.0, help="每条命令等应答秒数")
    args = ap.parse_args()

    if args.word:
        words = [int(args.word, 16)]
    elif args.words:
        words = [int(w.strip(), 16) for w in args.words.replace(",", " ").split()]
    else:
        words = list(SUSPECTS)
    payload = bytes.fromhex(args.payload) if args.payload else b""

    findings: dict[int, str] = {}
    with Probe(args.host, args.port) as probe:
        print(f"已连接 {args.host}:{args.port}  设备 {probe.device_id or '?'}")
        start = read_block(probe, args.wait)
        if start is None:
            print("读不到基线块，先确认空调在不在线")
            return 1
        base = start.raw
        print(f"基线块：{base.hex()}")
        print(f"待试 {len(words)} 个命令字，载荷 {payload.hex() or '（空）'}：\n")

        for word in words:
            detail = try_word(probe, word, payload, args.wait)
            if detail:
                findings[word] = detail
            time.sleep(0.5)

        final = read_block(probe, args.wait)
        if final:
            drift = [(i, base[i], final.raw[i]) for i in range(24)
                     if final.raw[i] != base[i] and i not in VOLATILE_IDX]
            print(f"\n收尾：{final.raw.hex()}")
            print("  与基线一致：" + ("✓ 是" if not drift
                                      else f"⚠ 否 → {[(f'c{i}', f'0x{a:02x}->0x{b:02x}') for i, a, b in drift]}"))

    print("\n" + "=" * 70)
    if findings:
        print("有副作用的命令字（可用来补功能）：")
        for word, detail in findings.items():
            print(f"  {word:04x}  {detail}")
        print("\n下一步：对「显示屏关 / 极速 / 静音 / 半度」这几个目标位，"
              "试 --payload 01/00 看能不能定向置位")
    else:
        print("这轮没发现任何命令字改动设置。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

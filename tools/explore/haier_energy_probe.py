#!/usr/bin/env python3
"""电量探测：56800 本地通道上到底有没有「电量 / 功率」？（一次性排查脚本）

背景：App 里能看到用电 ≠ 本地 EPP 能读到。
电量是室内机主板测的，从主板到外面有两条出口：
  ① 模块主动上报到云端 —— App 看的是这条（加密、且是模块自己发起的出网连接，我们抓不到）
  ② 本地 EPP 应答 —— 我们这套 56800 / smartair2 方言，本脚本只问这条

电量若存在，只可能藏在三处，脚本三处都试：

  ① 命令字    hOn 的 4dfe（GET_BIG_DATA，含 0~65535W 功率）、4c01（按参数 ID 查）、4d00~4dff 全扫
  ② 帧类型    0x61 版本 / 0x70 设备 ID / 0x7c 配置 / 0xf7 网络上报 / 0xfc 管理 …
  ③ 24 字节块 已知字段之外还有 c4 c6~c10 c12 c14 c18 c20~c22 未知；
              --watch 每 30s 记一次，拿去跟 App 的用电曲线对表

安全：只发「读」类命令。已知会改状态的 4d02 / 4d03（开关机）、4d5f（设置）在 --sweep 里也会跳过。

⚠️ 2026-10-17 实测教训：全扫 4d00~4dff 时，4d04/4d08/4d09/4d18/4d19/4d1e/4d1f/4d22/4d23/4d24
这些字虽然回 STATUS，但**状态块跟着变了**（c16 bit7 遥控锁被置上、c17 健康位翻转、c19 出现 0x01），
看起来是「字段级 set」而非纯读。现在这些字默认列入 SIDE_EFFECT_WORDS 跳过，要试必须 --force-unknown；
脚本跑完会自动比对前后状态块并提示。

用法：
  python3 haier_energy_probe.py 192.168.1.100              # 探一小撮（默认，约 30 秒）
  python3 haier_energy_probe.py 192.168.1.100 --sweep      # 扫 4d00~4dff（约 5 分钟）
  python3 haier_energy_probe.py 192.168.1.100 --types      # 只扫帧类型
  python3 haier_energy_probe.py 192.168.1.100 --watch 600  # 监听 10 分钟，每 30s 采一次 24 字节

注意：模块**同时只接受一个 TCP 连接** —— 跑之前把手机 App 里这台空调划掉。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from haier_client import HaierClient, HaierError                          # noqa: E402
from haier_protocol import (ADDR_AC, ADDR_MODULE, FRAME_NAMES,            # noqa: E402
                            HaierStatus, build_request, epp_frame)

# 协议层没定义的帧名，只为看懂日志（不影响实现）
EXTRA_NAMES = {0x70: "GET_DEV_ID", 0x71: "DEV_ID_RESP", 0x7C: "GET_CONFIG",
               0x7D: "CONFIG_RESP", 0xFC: "GET_MGMT", 0xFD: "MGMT_RESP",
               0xF7: "NET_REPORT"}

# 24 字节块里「已知」的下标；其余都是还没对出来的字节 → 电量若在块里，只可能在它们中间
KNOWN_IDX = {0, 1, 2, 3, 5, 11, 13, 15, 16, 17, 19, 23}

# 实时量（室温 / 湿度 / 主板交换计数）：本来就会自己变，比对"有没有副作用"时要排除
VOLATILE_IDX = {1, 3, 5}

# 会改状态、探测时一律跳过
WRITE_WORDS = {0x4D02, 0x4D03, 0x4D5F}

# 2026-10-17 实测（KFR-35G/BAA21A1U）：全扫 4d00~4dff 时，下面这些字虽然回 STATUS，
# 但状态块跟着变了（c16 出现 0x80=遥控锁、c17 健康位翻转、c19 出现 0x01），
# 怀疑是「字段级 set」而不是纯读 → 默认一律不碰，要试必须加 --force-unknown。
SIDE_EFFECT_WORDS = {0x4D04, 0x4D08, 0x4D09, 0x4D18, 0x4D19,
                     0x4D1E, 0x4D1F, 0x4D22, 0x4D23, 0x4D24}

# 默认探一小撮（只读）：4d01 是对照组（一定通），其余是 hOn 方言里最可能有电量的几个
CURATED_WORDS = [0x4D01, 0x4D00, 0x4C00, 0x4C01, 0x4DFD, 0x4DFE, 0x4DFF]

# 模块/板子级的查询帧（hOn 文档里的命令字 + 我们已经在用的 0x73）
CURATED_TYPES = [(0x61, b"\x00\x07", ADDR_MODULE), (0x61, b"", ADDR_MODULE),
                 (0x70, b"", ADDR_MODULE), (0x7C, b"", ADDR_MODULE),
                 (0xF7, b"", ADDR_MODULE), (0xFC, b"", ADDR_MODULE)]

HITS: list[str] = []


def name_of(frame_type: int) -> str:
    return FRAME_NAMES.get(frame_type) or EXTRA_NAMES.get(frame_type) or f"0x{frame_type:02x}"


def unknown_bytes(block: bytes) -> str:
    """把 24 字节块里「非 0 且未知」的字节列出来。"""
    return " ".join(f"c{i}=0x{block[i]:02x}" for i in range(min(24, len(block)))
                    if i not in KNOWN_IDX and block[i])


class Probe(HaierClient):
    """在 HaierClient 上开个后门：发一帧，把回来的东西（含 INVALID、无应答）原样收下。"""

    def ask(self, frame: bytes, wait: float) -> tuple[list, bool]:
        self._seq = (self._seq + 1) & 0xFF
        seq = self._seq
        try:
            self._sock.sendall(build_request(frame, seq=seq, mac=self.mac))   # type: ignore[union-attr]
        except OSError as exc:
            raise HaierError(f"发送失败：{exc}") from exc
        deadline = time.monotonic() + wait
        got: list = []
        try:
            while time.monotonic() < deadline:
                got.extend(self._drain(deadline))
                if any(e.seq == seq for e in got):       # 已拿到本条的应答，不用等满
                    break
        except HaierError as exc:
            print(f"      ! {exc}")
        mine = [e for e in got if e.seq == seq]
        return (mine or got), bool(mine)          # 序号对不上也把杂帧带回来看看


def report(label: str, envs: list, matched: bool) -> str:
    """打印一条探测结果，返回分类：none / invalid / status / other。"""
    if not envs:
        print(f"  {label:<22} 无应答（连 INVALID 都没回）")
        return "none"
    for env in envs:
        frame = env.frame
        extra = ""
        if frame.frame_type in (0x02, 0x06) and len(frame.data) >= 26:
            extra = f"   未知字节: {unknown_bytes(frame.data[2:26]) or '（全 0）'}"
            if len(frame.data) > 26:                     # 比标准状态块长 → 有额外载荷
                tail = frame.data[26:]
                nonzero = " ".join(f"{idx + 24}=0x{value:02x}"
                                   for idx, value in enumerate(tail) if value)
                extra += f"   载荷多 {len(tail)} 字节，非 0 处: {nonzero or '（全 0）'}"
        print(f"  {label:<22} type=0x{frame.frame_type:02x}({name_of(frame.frame_type)}) "
              f"data({len(frame.data)})={frame.data.hex()}{extra}")
    types = {e.frame.frame_type for e in envs}
    if types == {0x03}:
        return "invalid"
    if types <= {0x02, 0x06} and all(len(e.frame.data) == 26 for e in envs):
        return "status"                                  # 就是标准 24 字节状态块，没啥新东西
    HITS.append(f"{label} → " + "; ".join(
        f"type=0x{e.frame.frame_type:02x}({name_of(e.frame.frame_type)}) data={e.frame.data.hex()}"
        for e in envs))
    return "other"


def ask_safe(probe: Probe, frame: bytes, wait: float) -> tuple[list, bool]:
    """带重连的 ask：模块中途抽风（限流/断连）不至于把整轮扫描打断。"""
    try:
        return probe.ask(frame, wait)
    except HaierError as exc:
        print(f"      ! {exc}")
        try:
            probe.close()
            probe.connect()
            print("      · 已重连，继续")
        except (OSError, HaierError) as exc2:
            print(f"      ! 重连失败：{exc2}")
        return [], False


def watch(host: str, port: int, seconds: float, interval: float) -> int:
    """定时读 24 字节块，盯未知字节会不会跟着负载走。"""
    print(f"监听 {seconds:.0f}s，每 {interval:.0f}s 一次 —— 现在打开 App 的用电页面，两边一起看。")
    seen: dict[int, set[int]] = {}
    samples = 0
    t0 = time.monotonic()
    with Probe(host, port) as probe:
        print(f"已连接 {host}:{port}  设备 {probe.device_id or '?'}")
        while time.monotonic() - t0 < seconds:
            stamp = time.monotonic() - t0
            try:
                status = HaierStatus.from_frame(
                    probe.request(epp_frame(b"\x4d\x01", addr=ADDR_AC)).frame)
            except (HaierError, ValueError) as exc:
                print(f"[{stamp:7.0f}s] 读失败：{exc} —— 5 秒后重连")
                time.sleep(5)
                try:
                    probe.close()
                    probe.connect()
                except OSError as exc2:
                    print(f"           重连失败：{exc2}")
                    time.sleep(interval)
                continue
            except OSError as exc:
                print(f"[{stamp:7.0f}s] socket 异常：{exc} —— 5 秒后重连")
                time.sleep(5)
                continue
            samples += 1
            for idx, value in enumerate(status.raw):
                seen.setdefault(idx, set()).add(value)
            print(f"[{stamp:7.0f}s] {status.raw.hex()}  "
                  f"{'开' if status.power else '关'} 设定{status.target_temperature:.0f}℃ "
                  f"室温{status.room_temperature:.0f}℃ 湿度{status.room_humidity}%  "
                  f"未知字节: {unknown_bytes(status.raw) or '（全 0）'}")
            time.sleep(interval)

    print(f"\n{samples} 个样本。**变化过**的未知字节（越像用电，越可能是它）：")
    moved = False
    for idx in sorted(seen):
        if idx in KNOWN_IDX or len(seen[idx]) < 2:
            continue
        moved = True
        values = "/".join(f"0x{v:02x}" for v in sorted(seen[idx]))
        print(f"  c{idx:<2} {values}")
    if not moved:
        print("  （没有未知字节变化过）")
    print("\n下一步：把上面变化过的字节拿去和 App 的实时功率对表 ——"
          " 随压缩机负载线性变化的就是功率；若守恒/单调递增则可能是累计量。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="探测 56800 本地通道上有没有电量 / 功率数据")
    ap.add_argument("host", help="空调 IP")
    ap.add_argument("--port", type=int, default=56800)
    ap.add_argument("--sweep", action="store_true", help="扫全部 4d00~4dff（约 5 分钟）")
    ap.add_argument("--types", action="store_true", help="扫帧类型")
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="监听模式：总时长（秒）")
    ap.add_argument("--interval", type=float, default=30.0, help="监听采样间隔（秒，默认 30）")
    ap.add_argument("--wait", type=float, default=2.0, help="每条命令等应答的秒数（默认 2）")
    ap.add_argument("--words", help="自定义命令字，逗号分隔（十六进制，如 4dfe,4c01）")
    ap.add_argument("--force-unknown", action="store_true",
                    help="连「疑似有副作用」的命令字也扫（会改空调设置，慎用）")
    args = ap.parse_args()

    if args.watch:
        return watch(args.host, args.port, args.watch, args.interval)

    counts: dict[str, int] = {}
    with Probe(args.host, args.port) as probe:
        print(f"已连接 {args.host}:{args.port}  设备 {probe.device_id or '?'}")
        if probe.burst:
            print("连接时模块主动推的帧：")
            for env in probe.burst:
                print(f"  type=0x{env.frame.frame_type:02x}({name_of(env.frame.frame_type)}) "
                      f"data({len(env.frame.data)})={env.frame.data.hex()}")

        # 探测前先留一份基线块：既是链路对照，也是「这次探测有没有副作用」的锚点
        base_status = probe.status_from_burst()
        if base_status is None:
            envs, _ = ask_safe(probe, epp_frame(b"\x4d\x01", addr=ADDR_AC), args.wait)
            base_status = HaierStatus.from_frame(envs[0].frame) if envs else None
        baseline = base_status.raw if base_status else None
        if baseline:
            print(f"\n探测前基线块：{baseline.hex()}")

        words = [int(w, 16) for w in args.words.replace(",", " ").split()] if args.words \
            else list(CURATED_WORDS)
        if args.sweep:
            args.wait = min(args.wait, 1.0)
            skip = WRITE_WORDS if args.force_unknown else WRITE_WORDS | SIDE_EFFECT_WORDS
            words = [w for w in range(0x4D00, 0x4E00) if w not in skip]
            print(f"\n[全扫] 4d00~4dff，跳过会改状态/疑似写命令的 "
                  f"{[hex(w) for w in sorted(skip)]}，预计 {len(words) * (args.wait + 0.25) / 60:.1f} 分钟")
        else:
            print("\n[命令字] 只读探测（4d01 是对照组）：")

        for word in words:
            if word in WRITE_WORDS:
                continue
            result = report(f"{word:04x}", *ask_safe(
                probe, epp_frame(word.to_bytes(2, "big"), addr=ADDR_AC), args.wait))
            counts[result] = counts.get(result, 0) + 1
            time.sleep(0.25)
        print(f"\n命令字统计：{counts}")

        if args.types or not args.words:
            print("\n[帧类型] 模块 / 板子级查询：")
            for frame_type, payload, addr in CURATED_TYPES:
                report(f"type 0x{frame_type:02x} {name_of(frame_type)}", *ask_safe(
                    probe, epp_frame(payload, frame_type=frame_type, addr=addr), args.wait))
                time.sleep(0.25)

        # 收尾：再读一次，比对有没有动到空调设置
        if baseline:
            envs, _ = ask_safe(probe, epp_frame(b"\x4d\x01", addr=ADDR_AC), args.wait)
            after = HaierStatus.from_frame(envs[0].frame).raw if envs else None
            if after:
                changed = [(i, baseline[i], after[i]) for i in range(24)
                           if after[i] != baseline[i] and i not in VOLATILE_IDX]
                if changed:
                    print("\n⚠️  探测过程中状态块被改动了（说明扫到了有副作用的命令字）：")
                    for idx, before, now in changed:
                        print(f"      c{idx}: 0x{before:02x} -> 0x{now:02x}")
                    print(f"      基线 {baseline.hex()}\n      现在 {after.hex()}")
                    print("      → 要还原：把被改的字节按「读回当前块 → 改回基线值 → 4d5f 回填」写回去")
                else:
                    print(f"\n✓ 探测前后状态块一致（{after.hex()}），没动到空调设置"
                          f"（室温/湿度/cntrl 这类实时值已忽略）。")

    print("\n" + "=" * 68)
    if HITS:
        print("值得深挖的应答（非 INVALID、非普通状态）：")
        for line in HITS:
            print(f"  ★ {line}")
    else:
        print("没有拿到任何「非 INVALID / 非普通状态」的应答。")
    print("下一步：\n"
          "  · 有 ★ → 把原始字节贴出来，一起对字段（很可能就是功率/累计电量）\n"
          "  · 没 ★ → 再跑一次 --watch 600，看未知字节跟不跟 App 的用电曲线走\n"
          "  · 两边都没有 → 本地通道基本没戏，转外部计量（带计量的 16A 插座 / PZEM / Shelly EM）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""功能探测：逐个字段改一下、看空调是否接受并回读、然后立刻还原。

每项都：
  ① 读基线 → ② 改一项 → ③ 看回执是否变了 → ④ 用「基线原始块」整块还原

用法：python3 haier_caps.py 192.168.1.100
"""
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from haier_client import HaierClient, _fmt          # noqa: E402  根目录兼容层
from haier_protocol import HaierStatus              # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"


def probe(cli: HaierClient, name: str, mutate, check) -> None:
    baseline = cli.read_status()
    base_raw = baseline.raw
    try:
        after = cli.apply(mutate(baseline))
    except Exception as exc:                     # noqa: BLE001
        print(f"  {name:<14} ✗ 发送失败：{exc}")
        return
    accepted = check(after)
    mark = "✓ 支持" if accepted else "✗ 未生效"
    detail = f"回读 {after.raw.hex()}"
    print(f"  {name:<14} {mark}   {detail}")
    # 整块还原成基线
    back = cli.apply(HaierStatus(bytes(base_raw)))
    if back.raw != base_raw:
        print(f"  {name:<14} ⚠ 还原后与基线不一致：{back.raw.hex()} vs {base_raw.hex()}")
    time.sleep(0.8)


def main() -> int:
    with HaierClient(HOST) as cli:
        base = cli.read_status()
        print(f"基线：{_fmt(base)}\n  原始块：{base.raw.hex()}\n")
        print("逐项探测（每项改完立刻还原）：")

        probe(cli, "半度位 27.5℃",
              lambda s: s.with_target(27.5),
              lambda s: abs(s.target_temperature - 27.5) < 0.01)

        probe(cli, "健康 关",
              lambda s: s.with_health(False),
              lambda s: not s.health)

        probe(cli, "显示屏 关",
              lambda s: s.with_bits(19, 0x20, True),
              lambda s: s.display_off)

        probe(cli, "摆风 c15=1",
              lambda s: s.with_byte(15, 1),
              lambda s: s.swing == 1)

        probe(cli, "极速 turbo",
              lambda s: s.with_bits(19, 0x02, True),
              lambda s: s.preset == "turbo")

        probe(cli, "静音 quiet",
              lambda s: s.with_bits(19, 0x04, True),
              lambda s: s.preset == "quiet")

        probe(cli, "遥控锁 c16=0x80",
              lambda s: s.with_bits(16, 0x80, True),
              lambda s: bool(s.raw[16] & 0x80))

        time.sleep(1)
        final = cli.read_status()
        print(f"\n收尾：{_fmt(final)}")
        print(f"  原始块：{final.raw.hex()}")
        print("  与基线一致：", final.raw == base.raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())

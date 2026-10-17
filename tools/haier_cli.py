#!/usr/bin/env python3
"""命令行工具：直接用本地协议读写空调（不经过 Home Assistant）。

    python3 tools/haier_cli.py 192.168.1.100 read
    python3 tools/haier_cli.py 192.168.1.100 on / off
    python3 tools/haier_cli.py 192.168.1.100 temp 26
    python3 tools/haier_cli.py 192.168.1.100 mode cool
    python3 tools/haier_cli.py 192.168.1.100 fan auto
    python3 tools/haier_cli.py 192.168.1.100 swing on
    python3 tools/haier_cli.py 192.168.1.100 health off
    python3 tools/haier_cli.py 192.168.1.100 fault

每次操作都新开一个短连接（模块同时只接受一个连接，别并发）。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import component_loader  # noqa: F401,E402  导入即把集成里的 protocol/client 装进 sys.modules
import local_config  # noqa: E402  本地私有配置：真实 IP/MAC 放 local_settings.py（不入库）
from haier_esdk.client import HaierClient, HaierError, format_status  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="海尔空调本地直连 CLI")
    parser.add_argument("host", nargs="?",
                        default=local_config.HOST,
                        help="空调 IP（省略则取 local_settings.py / local_config.py 里的值）")
    parser.add_argument("command",
                        choices=["read", "on", "off", "temp", "mode", "fan", "swing", "health",
                                 "fault"],
                        nargs="?", default="read")
    parser.add_argument("value", nargs="?", help="temp 的摄氏温度 / mode / fan / swing / health 的取值")
    args = parser.parse_args()

    try:
        with HaierClient(args.host, port=local_config.PORT, mac=local_config.MAC) as client:
            if args.command == "read":
                print(format_status(client.read_status()))
            elif args.command == "fault":
                bitmap = client.read_fault()
                print(f"故障位图 {bitmap.hex()} → {'无故障' if not any(bitmap) else '有故障'}")
            elif args.command == "on":
                print(format_status(client.set_hvac(power=True)))
            elif args.command == "off":
                print(format_status(client.set_hvac(power=False)))
            elif args.command == "temp":
                if args.value is None:
                    parser.error("temp 需要一个温度值，例如 26")
                print(format_status(client.set_hvac(temperature=float(args.value))))
            elif args.command == "mode":
                print(format_status(client.set_hvac(mode=args.value)))
            elif args.command == "fan":
                print(format_status(client.set_hvac(fan=args.value)))
            elif args.command == "swing":
                print(format_status(client.set_hvac(swing=(args.value != "off"))))
            elif args.command == "health":
                print(format_status(client.set_hvac(health=(args.value == "on"))))
    except HaierError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

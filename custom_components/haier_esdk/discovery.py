"""IP 变了也能自己找回来：扫本地网段的 56800，用协议里的设备 ID（MAC）认人。

背景：空调的 DHCP 租约一换 IP，写死的 ``host`` 就连不上了。模块既不回 mDNS 也不认
UDP 广播（UDISCOVERY 实测无应答），但它**每次连上都会在信封头里带上自己的 MAC**
（12 字节 ASCII，就是集成用来当唯一 ID 的那个值）。所以「IP 变了」这件事可以自愈：

1. 只在**轮询已经连不上**的时候才扫，而且两次扫描之间有冷却（不能每 30 秒扫一遍）；
2. 先并发 TCP 连一遍候选地址，只有连得上的才读设备 ID（不做广播、不发任何写命令）；
3. 设备 ID 对上才认，认到了由协调器把条目里的 ``host`` 改掉 —— 不是「随便找台
   海尔设备就改」，避免多台空调时改错。

这个模块只依赖标准库 + ``.client``，不碰 homeassistant（方便命令行工具和测试直接用）。
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .client import DEFAULT_MAC, HaierClient, HaierError
from .const import DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 0.4        # 探测单台地址的 TCP 超时（连不上就赶紧换下一台）
PROBE_TIMEOUT = 1.5          # 连上之后等模块主动推设备 ID 的时间（LAN 上通常几十毫秒）
PROBE_WORKERS = 64           # 并发探测线程数
MAX_HOSTS = 1024             # 单个网段最多这么多地址（/22）；更大的网段退回只扫本机 /24
MAX_SCAN_HOSTS = 2048        # 一次扫描的地址总数上限（多网卡时的安全阀）
MAX_NETWORKS = 8             # 最多扫几张网卡


@dataclass(frozen=True)
class Probe:
    """一次成功探测的结果。"""

    host: str
    device_id: str | None


# ---------------------------------------------------------------------- 纯函数部分
def looks_like_device_id(value: str | None) -> bool:
    """设备 ID 就是 12 字节 ASCII 的模块 MAC（集成里就是这么存的）。

    配置时如果模块没推 MAC，流程会退化成 ``host-<ip>``，那种值不能用来认人。
    """
    if not value:
        return False
    value = value.strip()
    return len(value) == 12 and value.isascii() and value.isalnum()


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _to_network(item: Any) -> ipaddress.IPv4Network | None:
    """把 HA ``network`` 集成给的地址项（``{"address": "192.168.1.5/24"}``）变成网段。"""
    if not isinstance(item, dict):
        return None
    address = str(item.get("address") or "").strip()
    if not address:
        return None
    if "/" not in address:
        prefix = item.get("network_prefix")
        address = f"{address}/{int(prefix if prefix is not None else 24)}"
    try:
        return ipaddress.ip_network(address, strict=False)
    except ValueError:
        return None


def networks_from_adapters(adapters: Iterable[Any]) -> list[ipaddress.IPv4Network]:
    """从网卡信息里挑出真实存在的 IPv4 网段。"""
    out: list[ipaddress.IPv4Network] = []
    for adapter in adapters or []:
        if not isinstance(adapter, dict) or not adapter.get("enabled", True):
            continue
        for item in adapter.get("ipv4") or []:
            network = _to_network(item)
            if network is not None and network not in out:
                out.append(network)
    return out


def fallback_network(hint_host: str | None = None) -> ipaddress.IPv4Network | None:
    """拿不到网卡信息时，猜一下本机在哪张网里。

    做法是 UDP ``connect``（不发包，只让内核按路由挑一张网卡），读出本机源地址，
    再按 /24 切网段 —— 家庭网络基本都够用。
    """
    for target in (hint_host, "8.8.8.8"):
        if not target:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((str(target), 9))
            local_ip = sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
        try:
            iface = ipaddress.ip_address(local_ip)
        except ValueError:                       # pragma: no cover - 理论上不会发生
            continue
        return ipaddress.ip_network(f"{iface}/24", strict=False)
    return None


def local_networks(adapters: Iterable[Any] | None,
                   hint_host: str | None = None) -> list[ipaddress.IPv4Network]:
    """要扫的网段：优先网卡上的私网 IPv4，实在没有就退回本机 /24。"""
    networks = [n for n in networks_from_adapters(adapters or [])
                if n.is_private and n.num_addresses <= MAX_HOSTS]
    if not networks:
        fallback = fallback_network(hint_host)
        if fallback is not None:
            _LOGGER.debug("没有可用的网卡网段，退回只扫 %s", fallback)
            networks = [fallback]
    return networks[:MAX_NETWORKS]


def expand_hosts(networks: Sequence[ipaddress.IPv4Network],
                 limit: int = MAX_SCAN_HOSTS) -> list[str]:
    """把网段摊开成具体地址（不含网络号 / 广播地址），总数封顶 ``limit``。"""
    out: list[str] = []
    for network in networks:
        for ip in network.hosts():
            if len(out) >= limit:
                _LOGGER.debug("地址数超过上限 %d，剩下的网段这次不扫了", limit)
                return out
            out.append(str(ip))
    return out


# ---------------------------------------------------------------------- 探测部分
def probe_host(host: str, port: int = DEFAULT_PORT, mac: bytes | str = DEFAULT_MAC,
               timeout: float = CONNECT_TIMEOUT) -> Probe | None:
    """连一次、读设备 ID；连不上或不是海尔模块就返回 None。

    ``timeout`` 是 **TCP 连接**超时（扫网段时越短越好，连不上就换下一台）；
    连上之后再给 ``PROBE_TIMEOUT`` 收设备 ID。只读，不发任何写命令。
    """
    try:
        with HaierClient(host, port, mac=mac, timeout=timeout,
                         burst_wait=PROBE_TIMEOUT) as client:
            device_id = client.device_id
            if device_id is None:
                try:
                    client.read_status()
                except (HaierError, OSError):
                    return None
            return Probe(host, device_id)
    except (HaierError, OSError, TimeoutError) as exc:
        _LOGGER.debug("探测 %s:%s 不算数：%s", host, port, exc)
        return None


def scan(hosts: Iterable[str], port: int = DEFAULT_PORT, mac: bytes | str = DEFAULT_MAC,
         probe: Callable[[str, int, bytes | str, float], Probe | None] = probe_host,
         workers: int = PROBE_WORKERS, timeout: float = CONNECT_TIMEOUT) -> list[Probe]:
    """并发探测一批地址，返回所有应答的（顺序与输入一致，方便复现）。"""
    host_list = [h for h in hosts]
    if not host_list:
        return []
    found: list[Probe] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(host_list)))) as pool:
        futures = [pool.submit(probe, h, port, mac, timeout) for h in host_list]
        for future in futures:
            try:
                result = future.result()
            except Exception as exc:              # noqa: BLE001 单台出错不影响整体
                _LOGGER.debug("探测任务异常：%s", exc)
                continue
            if result is not None:
                found.append(result)
    return found


def select_device(found: Sequence[Probe], device_id: str | None) -> str | None:
    """从探测结果里挑出目标设备，挑不准就返回 None（宁可不改，也不改错）。"""
    if not found:
        return None
    if looks_like_device_id(device_id):
        matched = [p for p in found if _norm(p.device_id) == _norm(device_id)]
        if not matched:
            _LOGGER.warning("网段里扫到 %d 台海尔设备，但没有 %s：%s", len(found), device_id,
                            [f"{p.host}/{p.device_id}" for p in found])
            return None
        if len(matched) > 1:                      # pragma: no cover - 理论上不可能
            _LOGGER.warning("设备 ID 撞了（%s），取第一个 %s", device_id, matched[0].host)
        return matched[0].host
    # 条目里的设备 ID 不可用（配置时没读到 MAC）：只有唯一一台应答者才敢认
    if len(found) == 1:
        return found[0].host
    _LOGGER.warning("设备 ID 不明确，又扫到 %d 台设备，不敢乱改 IP：%s",
                    len(found), [p.host for p in found])
    return None


# ---------------------------------------------------------------------- 给协调器用
def _adapters(hass: Any) -> list[Any]:
    """网卡列表（HA 的 network 集成提供）；拿不到就走 fallback，不影响轮询。

    这里 import 了 HA 的 ``network`` 集成 —— manifest 里对应声明
    ``after_dependencies: ["network"]``（hassfest 会查静态 import，漏了直接报错）。
    """
    try:
        from homeassistant.components.network import async_get_adapters
    except ImportError:                           # pragma: no cover - 老版本 / 测试环境
        return []
    try:
        return list(async_get_adapters(hass))
    except Exception as exc:                      # noqa: BLE001
        _LOGGER.debug("读不到网卡信息：%s", exc)
        return []


async def async_find_device(hass: Any, device_id: str | None, port: int = DEFAULT_PORT, *,
                            hint_host: str | None = None, workers: int = PROBE_WORKERS,
                            timeout: float = CONNECT_TIMEOUT) -> str | None:
    """扫本地网段找 ``device_id`` 这台空调，返回它的新 IP；找不到返回 None。"""
    adapters = _adapters(hass)
    networks = await hass.async_add_executor_job(local_networks, adapters, hint_host)
    hosts = expand_hosts(networks)
    if not hosts:
        _LOGGER.debug("没有可扫的网段（是不是没开 network 集成？）")
        return None
    mac: bytes | str = device_id if looks_like_device_id(device_id) else DEFAULT_MAC
    _LOGGER.debug("在 %s 上扫 %d 个地址，找设备 %s", [str(n) for n in networks], len(hosts),
                  device_id or "（未知）")
    found = await hass.async_add_executor_job(scan, hosts, port, mac, probe_host, workers, timeout)
    return select_device(found, device_id)


__all__ = ["CONNECT_TIMEOUT", "MAX_HOSTS", "MAX_SCAN_HOSTS", "PROBE_TIMEOUT", "PROBE_WORKERS",
           "Probe", "async_find_device", "expand_hosts", "fallback_network", "local_networks",
           "looks_like_device_id", "networks_from_adapters", "probe_host", "scan",
           "select_device"]

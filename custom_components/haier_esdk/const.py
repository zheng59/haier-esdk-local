"""海尔 eSDK 本地直连集成的常量。"""
from __future__ import annotations

DOMAIN = "haier_esdk"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_MAC = "mac"
CONF_DEVICE_ID = "device_id"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_REDISCOVER = "rediscover"

DEFAULT_PORT = 56800
DEFAULT_SCAN_INTERVAL = 30
# 空调 DHCP 换 IP 后自动去网段里把它找回来（认模块 MAC），默认开
DEFAULT_REDISCOVER = True
DEFAULT_NAME = "海尔空调"
MANUFACTURER = "Haier"
# 这是空调换掉的「Wi-Fi 模块」型号族；客厅这台是 KZW 一代 eSDK 固件
MODULE_MODEL = "eSDK Wi-Fi 模块（KZW 一代 / 56800 明文 EPP）"

PLATFORMS = ["climate", "binary_sensor", "switch"]

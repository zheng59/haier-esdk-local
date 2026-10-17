"""本地私有配置模板（会提交进仓库）。

用法：

    cp local_settings.example.py local_settings.py

``local_settings.py`` 已被 .gitignore 忽略，只留在你本机，随你怎么填。
下面三项是命令行工具 / 探索脚本默认使用的值。
"""

HOST = "192.168.1.100"      # 空调在局域网里的 IP
PORT = 56800               # 一代 eSDK 模块的明文 EPP 端口
MAC = "001122334455"       # 模块 MAC（12 位 ASCII，同时也是设备 ID）

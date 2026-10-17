# 海尔空调本地直连（Home Assistant 自定义集成）

把家里那台海尔空调接进 Home Assistant，**全程本地**：不加云端、不改硬件、不拆机。
HA ↔ 空调 IP:56800，明文私有协议（EPP），没有任何账号 / 密钥 / 握手。

> 实测设备：海尔 **KFR-35G/BAA21AU1**（挂的是 KZW 一代 eSDK Wi-Fi 模块，固件 `e_1.2.02`）
> 集成域名 `haier_esdk`，模块型号族「eSDK 一代 / 56800 明文 EPP」。

> [!IMPORTANT]
> **免责声明**：这是一个**独立的社区项目，与海尔（Haier）没有任何关联，未获其授权或背书**。
> "Haier / 海尔" 及其它商标归各自权利人所有，此处仅用于说明本软件所互操作的硬件。
> 本项目只与你**自己拥有**、**在你自己局域网内**的设备通信，全程明文，不涉及账号、密钥，
> 也不调用任何云端接口，更没有绕过任何技术保护措施。软件按「原样」提供，
> 不对设备损坏、数据丢失或任何后果负责 —— **使用风险自负**。

---

## 1. 为什么这台不用捣鼓 localKey

海尔的 Wi-Fi 模块有好几代，**只有一代 eSDK 的 56800 端口是明文透传**：

| 代 | 端口 | 要不要密钥 |
| --- | --- | --- |
| **eSDK 一代**（本机） | **56800** | **不要**，连上就能收发 |
| uSS / 二代（hOn App 那代） | 12334 | 要 localKey，得先从云上取 |

本机连上 56800 后模块会**主动推**一帧故障位图 + 一帧完整状态，随后静默；
发一条读状态请求就回一条状态 —— 不需要任何认证。

## 2. 能做什么（都在真机上逐条验过）

| 能力 | 状态 | 备注 |
| --- | --- | --- |
| 开关机 | ✅ | 专用命令 `4D02/4D03` |
| 模式：自动 / 制冷 / 制热 / 除湿 / 送风 | ✅ | `4D5F` 回填控制块 |
| 设定温度 16–30℃ | ✅ | **整度**；半度会被主板回 `INVALID` 拒掉 |
| 风速：低 / 中 / 高 / 自动 | ✅ | |
| 摆风（上下左右一起） | ✅ | 控制块 `c15`；写入时会顺手把 `c19` bit0 清 0（见 §5.4） |
| 健康模式 | ✅ | `c17` bit3 → 独立开关实体 |
| 遥控锁 | ✅ | `c16` bit7 → 独立开关实体（默认不启用） |
| 读室温 / 湿度 | ✅ | 主板自带传感器，随轮询更新 |
| 故障位图 | ✅ | 连接时白拿，暴露成 `故障告警` 二进制传感器（带置位号，见 §9） |
| 用电量 / 实时功率 | ❌ | 本地 EPP 方言里**没有这个字段**（已全扫验证，见 §11） |
| 显示屏开关 / 极速 / 静音 / 半度 / 华氏 / 十度 | ❌ | 主板收到即忽略或回 `INVALID`（`tools/haier_caps.py` 逐项验过） |
| 屏显开关（遥控器能关） | ❌ | 遥控器可关屏显，但**本地块里读不到、也没有对应命令字**（见 §5.4、§11） |
| 摆风细分（水平 / 垂直分开） | ❌ | `c19` bit3/bit4 被主板丢弃（bit0 本身可写） |

**闭环**：每条写命令都会回一帧带新状态的 STATUS，而且可以随时再读一次二次确认 ——
不是红外那种「盲发」。

## 3. 装到 Home Assistant

### 方式 A：HACS（自定义仓库）

1. HACS → 集成 → 右上角 `⋮` → *自定义存储库*
2. 填本仓库地址，类别选 **Integration**
3. 装好后重启 HA → 设置 → 设备与服务 → 添加集成 → 搜 `Haier eSDK Local`

### 方式 B：手动拷贝

把 `custom_components/haier_esdk/` 整个目录拷进 HA 的配置目录。

**Docker（容器里跑 HA）**

```bash
# 1. 找到 HA 容器名
docker ps --format '{{.Names}}\t{{.Image}}' | grep -i homeassistant

# 2. 拷进去（把 homeassistant 换成你的容器名）
docker exec homeassistant mkdir -p /config/custom_components
docker cp custom_components/haier_esdk homeassistant:/config/custom_components/

# 3. 重启并盯日志（看到「海尔空调已接入」就成）
docker restart homeassistant
docker logs -f homeassistant 2>&1 | grep -i -E 'haier|custom_components'
```

> 如果是 bind mount（`-v /宿主机/ha-config:/config`），直接往宿主机那个目录拷也行，
> 拷完照样重启容器。`docker cp` 进容器的文件属主是 root，标准镜像里 HA 就跑在 root 下，不用管。

**HA OS / Supervised**：拷到 `/config/custom_components/`（Samba 或 SSH add-on 都行）后重启。

拷完检查一下目录长这样（`manifest.json` 必须直接在这一层，不能再套一层文件夹）：

```
/config/custom_components/haier_esdk/
├── __init__.py  climate.py  switch.py  binary_sensor.py
├── protocol.py  client.py  coordinator.py  config_flow.py  diagnostics.py  const.py
├── manifest.json  strings.json
└── translations/zh-Hans.json  translations/en.json
```

### 填什么

| 字段 | 值 |
| --- | --- |
| IP 地址 | 空调在局域网里的 IP（例如 `192.168.1.100`） |
| 端口 | `56800`（默认，不用改） |
| 轮询间隔 | 默认 30 秒。**别低于 10 秒**：模块同时只接受一个 TCP 连接，且对高频连接有短时限流 |

保存时集成会现场连一次做验证，并自动从模块里读出设备 ID（MAC `001122334455`）作为
唯一标识 —— 所以同一台空调不会被重复添加。

> **IP 建议固定**：在路由器里给空调的 MAC 做静态 DHCP 绑定，否则换 IP 后要在
> 集成里改（集成 → 配置 → 选项 → 改 IP，会现场验活）。

> 要求 HA ≥ 2024.11（实测环境 2026 版一路可用）。

### 装完怎么自查

1. 日志里应有：`海尔空调已接入：192.168.1.100（设备 001122334455）`
2. 设备页会多出一个「Haier 空调」设备，带 1 个 climate + 3 个子实体
3. `climate.海尔空调` 的属性里能看到实时室温/湿度 —— 说明轮询通了
4. 点一下开关机，**空调会真的响**（写命令真到了主板，不是假应答）

## 4. 装完长什么样

- **climate.海尔空调**：开关 / 模式 / 设定温度 / 风速 / 摆风，附带实时室温与湿度
- **switch.健康模式**
- **switch.遥控锁**（默认隐藏，需要在实体的「已禁用的实体」里启用）
- **binary_sensor.故障告警**（默认隐藏；位图非全 0 即告警）

## 5. 协议速查（给自己以后维护用）

### 5.1 外层信封（80 字节头 + EPP 帧）

```
魔数(4) | 0×36 | MAC(12 ASCII) | 0×20 | 序号(BE32) | 帧长(BE32) | EPP 帧
```

- **请求方向魔数 = `00 00 27 14`**，模块→客户端 = `00 00 27 15`。
  用错方向模块**不报错、也不转发**，直接把帧吞掉 —— 这是本项目踩过的最大的坑。
- MAC 12 字节 ASCII 在偏移 40–51（`303031313232333334343535` = `001122334455`），
  同时也是「设备 ID」，可以直接拿来当 HA 的唯一标识。
- 序号 BE32 在偏移 72–75，应答里原样回传；帧长 BE32 在偏移 76–79。

### 5.2 EPP 帧

```
ff ff | L | addr(6) | type | data | checksum
L        = 6 + 1 + len(data) + 1
checksum = (L + addr + type + data) & 0xFF       ← 累加和，不是补码
```

| type | 名字 | 说明 |
| --- | --- | --- |
| `0x01` | CONTROL | 客户端下发 |
| `0x02` | STATUS | 状态应答（data = 命令字 + 24 字节控制块） |
| `0x03` | INVALID | **命令被拒**（例如设了不支持的温度） |
| `0x04` | ALARM | 故障位图（连接时主动推） |
| `0x06` | REPORT | 状态上报（连接时主动推） |
| `0x61` | GET_VERSION | 协议表里的名字，但**本机实测回 `INVALID`**（真机不给版本；`0x62` 也只是留在名字表里） |
| `0x73` / `0x74` | GET_ALARM / 应答 | 读故障位图 |

地址用 `00…00` 或 `00…01` 都能通（`ADDR_AC` / `ADDR_MODULE`）。

### 5.3 命令字

| 命令 | 含义 |
| --- | --- |
| `4D01` | 读状态 |
| `4D02` / `4D03` | 开机 / 关机 |
| `4D5F` | **设置**：payload = `4D5F` + 24 字节控制块，整块回填 |
| `4D08` / `4D09` | 健康模式 **关 / 开**（字段级命令） |
| `4D18` / `4D19` | 遥控锁 **关 / 开** |
| `4D1E` / `4D1F` | `c19` bit0（「改用 c19 判摆风」的总闸）**关 / 开** |

> **字段级命令**（2026-10-17 全扫 `4d00~4dff` 才挖出来）：除 `4d01` 外只有 11 个字会应答，
> 其中上面 6 个是「把一个字段置成固定值」——**偶数字 = 关、奇数字 = 开**，不带载荷。
> `4d04 / 4d22 / 4d23 / 4d24` 也会回 STATUS，但控制块看不出变化（目标位不在这 24 字节里，或主板不支持）。
> 本机没有对应「显示屏 / 极速 / 静音」的字，所以那几项仍然拿不回来。
> ⚠️ 这些字**有副作用**（会真改设置），别乱扫 —— 见 §9 与探测脚本内置的黑名单。

### 5.4 24 字节控制块（`data[2:26]`）

| 下标 | 含义 |
| --- | --- |
| `c1` | 室温（℃） |
| `c3` | 湿度（%） |
| `c5` | cntrl —— **发送时必须置 0**；主板自己会用它记交换计数（回读会看到 1、2…） |
| `c11` | 模式：0 自动 / 1 制冷 / 2 制热 / 3 送风 / 4 除湿 |
| `c13` | 风速：0 高 / 1 中 / 2 低 / 3 自动 |
| `c15` | 摆风（1 = 上下左右都摆；本位为 0 时由 c19 的位决定） |
| `c16` | bit3 华氏 / **bit7 遥控锁** |
| `c17` | **bit0 开关** / bit3 健康 / bit4 压缩机（本机一直读到 0）/ bit5 半度 / bit6 十度 |
| `c19` | bit0 用 c19 的摆风位 / bit1 极速 / bit2 静音 / bit3 水平摆风 / bit4 垂直摆风 / bit5 显示屏关 |

> `c19` bit5（显示屏关）**本机不适用**：写进去被主板丢弃，而且用遥控器关屏显后，这个 24 字节块
> **逐字节不变** → 屏显状态根本不在这条通道里（详见 §11）。
| `c23` | 设定温度 = 值 + 16 |

> 模式 `c11` 本机只有 0~4（写 `0x05/0x06/0x07` 一律回 `INVALID`）。**遥控器上的「PMV」（App 里同名模式）
> 在设备端就是 `c11=0`（自动）+ 风速自动（`c13=3`）+ 健康关** —— 块里**没有任何独立的 PMV 标志位**，
> 所以 HA 里看到 `auto` 就是它。（2026-10-17 实测：用**遥控器**切 PMV 后逐字节比对，不经过云端）

> 写操作的通用姿势（和 ESPHome 的 smartair2 一致）：
> **先读回当前 24 字节 → 只改要改的字段 → `c5` 归零 → `4D5F` 整块回填**。
> 只发 `4D5F` 不带块、或块里字段瞎猜，主板都不会理你。

> `c19` bit0 是「改用 `c19` 的 bit3/bit4 判摆风」的总闸，App / 遥控器会把它置上；
> 一旦置上主板就**忽略 `c15`**。本机 bit3/bit4 写了会被丢，所以写摆风时一律把 bit0 清 0，
> 保证 `c15` 说了算（`HaierStatus.with_swing()` 已内置，真机验过：开→`c15=1`、关→`c15=0`）。

### 5.5 模块脾气

- **同时只有一个 TCP 连接**；连上就被推一帧状态（等于白拿一次读数）。
- 快速重连会被限流（表现为推送阶段直接断/0 字节）。两次会话之间留几秒最稳。
- 被拒的命令回 `type=0x03 INVALID`（我们转成 `HaierCommandRejected` 抛出来）。

## 6. 命令行工具（不装 HA 也能用）

```bash
python3 tools/haier_cli.py 192.168.1.100 read          # 读状态
python3 tools/haier_cli.py 192.168.1.100 fault         # 读故障位图
python3 tools/haier_cli.py 192.168.1.100 off           # 关机
python3 tools/haier_cli.py 192.168.1.100 temp 26       # 设 26℃
python3 tools/haier_cli.py 192.168.1.100 mode dry      # 除湿
python3 tools/haier_cli.py 192.168.1.100 fan auto      # 风速自动
python3 tools/haier_cli.py 192.168.1.100 swing on      # 摆风
python3 tools/haier_caps.py 192.168.1.100              # 逐项探测主板支持哪些字段（改完自动还原）
```

想确认「电量 / 功率」在本地通道有没有（本研究项目的结论：没有，见 §11）：

```bash
python3 tools/explore/haier_energy_probe.py 192.168.1.100            # 只读探测 + 副作用自检
python3 tools/explore/haier_energy_probe.py 192.168.1.100 --sweep    # 全扫 4d00~4dff（已内置黑名单）
python3 tools/explore/haier_field_probe.py 192.168.1.100             # 逐个试「字段级命令」管哪一位（动完立刻还原）
```

### 本地配置（IP / 端口 / MAC）

上面的 IP 只是占位符。工具和探索脚本的真实取值来自仓库根目录的 **`local_settings.py`**
—— 它已被 `.gitignore` 忽略，**不会提交**：

```bash
cp local_settings.example.py local_settings.py    # 然后填上你的 IP / 端口 / MAC
```

取值优先级：`local_settings.py` → 环境变量 `HAIER_HOST` / `HAIER_PORT` / `HAIER_MAC`
→ `local_config.py` 里的占位符；**命令行参数始终最优先**（所以 `haier_cli.py <IP> read` 照旧可用）。

## 7. 代码结构

```
custom_components/haier_esdk/     ← 集成本体（对外分发就这一份）
├── protocol.py                   信封 / EPP / 24 字节控制块的编解码（纯函数，好测）
├── client.py                     同步 TCP 客户端（读 / 写 / set_hvac；HA 在执行器里跑它）
├── coordinator.py                轮询协调器：串行化 + 连接时顺手收故障位图
├── climate.py                    空调实体
├── switch.py                     健康模式 / 遥控锁
├── binary_sensor.py              故障告警（带位图与置位号）
├── diagnostics.py                诊断导出（连接参数 / 状态块 / 故障位图）
├── config_flow.py                填 IP → 现场验活 → 建设备；选项里可改 IP / 端口 / 轮询间隔
└── manifest.json / strings.json / translations/

component_loader.py              让测试和工具直接加载集成里的 protocol/client（造空壳包，
                                 绕过依赖 homeassistant 的 __init__.py）—— 保证**只有一份实现**
haier_protocol.py / haier_client.py   根目录兼容层（转发到上面那份，给老脚本用）
local_config.py                  本地配置加载器（真实 IP/MAC 放 local_settings.py，不入库）
local_settings.example.py        本地配置模板（复制成 local_settings.py 再填）
tools/                           命令行工具 + 能力探测
tools/explore/                   逆向/排查期的一次性脚本（存档备查：能量探测、字段级命令探测…）
tests/                           57 个单元测试
```

## 8. 跑测试

```bash
python3 -m unittest discover -s tests -v
```

57 个用例，全部离线（不碰真机）：

- **协议层 / 客户端**：校验和、信封布局、请求魔数、真机抓包的 216 字节突发解析、
  半条消息的拼接、控制块解码与改写、摆风写入顺带清 `c19` bit0、故障位图摊位号、
  `set_hvac` 的收发序列、`INVALID` 处理、对端关闭…（用假 socket 回放真机字节，不是 mock 掉业务逻辑）
- **实体层**：模式/风速双向映射、开关机语义、关机改温度自动开机、
  健康/遥控锁位、故障传感器（位图 + 置位号）、设备信息与唯一 ID（用最小 HA 替身）
- **诊断**：导出连接参数 / 状态块 / 故障位图（同样用最小 HA 替身）

## 9. 排查

| 现象 | 原因 / 处理 |
| --- | --- |
| 添加集成时报「连不上」 | IP 不对 / 不同网段 / 手机 App 或别的客户端正占着（模块只支持单连接，先把 App 划掉） |
| 实体长期「不可用」 | 轮询间隔太短被限流，改成 30 秒以上；或空调 IP 变了 |
| 改温度没反应 | 只支持整度 16–30；半度/越界会被 `INVALID` 拒掉（HA 里步进已固定 1℃） |
| 开关/模式改了但界面没变 | 下一轮轮询（≤30 秒）会拉回来；也可以在集成上手动「重新加载」 |
| 想确认主板到底支不支持某字段 | `python3 tools/haier_caps.py <空调IP>`（改一项验一项，立刻还原） |
| 换了空调 IP / 端口 | 集成 → **配置 → 选项**：改完会现场连一次验证，连不上不会保存（轮询间隔也在这改） |
| 想知道故障是什么 | `故障告警` 传感器的属性里有 `bitmap` 与 `set_bits`（置位号：`字节下标×8 + 位下标`，字节内低位在前）。位图含义厂家没公开，别自己猜故障名；日志里故障出现 / 清除各留一条 |
| 想要「用电量 / 功率」 | 本地通道没有这个数据（见 §11）。要么外部计量（计量插座 / PZEM / Shelly EM），要么走云端接口 |
| **别乱扫 `4dXX`** | 有些命令字是「字段级 set」：`4d08 / 4d19 / 4d1f` 实测会把遥控锁、健康位、`c19` 位改掉。要探就用手册里的 `tools/explore/haier_energy_probe.py`——它内置黑名单，并会自动比对前后状态块 |

## 10. 出处 / 参考

**本项目是独立实现**：代码、测试与探测脚本都是自己写的，只在「协议事实」层面参考了下列
公开资料（帧布局、命令字、位定义这类事实性信息不受著作权保护）。若某份资料未声明开源许可，
请注意我们**只参考了它记录的协议行为，没有复制其代码**。

- [esphome/esphome · `components/haier/smartair2_climate.cpp`](https://github.com/esphome/esphome/blob/dev/esphome/components/haier/smartair2_climate.cpp)
  —— 控制消息 `4D5F` + 24 字节块、`4D02/4D03`、能力位（**本机方言就是它的 smartair2 命令集**）。
  该文件是 C++，属 **GPLv3**；本项目未使用其任何代码。
- [KoalaBear84/HaierAC](https://github.com/KoalaBear84/HaierAC) —— 请求信封（魔数 `2714`、序号、
  帧长）的早期记录（该仓库**未声明许可**，仅参考其记录的协议行为）。
- [paveldn/HaierProtocol](https://github.com/paveldn/HaierProtocol)（MIT）与
  [paveldn/haier-esphome](https://github.com/paveldn/haier-esphome)（未声明许可）—— FrameType / e++ 帧层。
- [ddnlv/haier-kzw-w002-local](https://github.com/ddnlv/haier-kzw-w002-local)（MIT）—— 同为
  KZW 模块 + SmartAir2 的 Home Assistant 本地集成。
- [enapt/haismart-local](https://github.com/enapt/haismart-local)（MIT）—— uSS / 56800 的另一条路线
  （要 localKey）；`tools/explore/haier_probe.py` 的 uSS 握手格式参考其 `PROTOCOL.md`。
- [bstuff/haier-ac-remote](https://github.com/bstuff/haier-ac-remote) /
  [roeij/py-haier-ac-remote](https://github.com/roeij/py-haier-ac-remote) /
  [oxystin/homebridge-haier-air-conditioner](https://github.com/oxystin/homebridge-haier-air-conditioner)
  —— 更早的 56800 / SmartAir2 逆向工作。
- [Andre0512/hon](https://github.com/Andre0512/hon)（hOn 平台）、
  [banto6/haier](https://github.com/banto6/haier)（中国区云端）—— 其它海尔路线，本项目未采用。

## 11. 已知取舍

- 不做的：显示屏开关、极速、静音、半度、华氏、十度、水平/垂直摆风细分 —— 主板直接丢弃或回 `INVALID`
  （`tools/haier_caps.py` 逐项验过；又用「字段级命令」那条路复验过一次，没有对应的命令字）。
  压缩机状态也读不到（`c17` bit4 一直是 0），`hvac_action` 按模式推断。
- 屏显开关（**独立实测过，结论是本地做不到**）：遥控器能关屏显，但关完之后这个 24 字节控制块
  **逐字节不变**（只有室温/湿度/计数在动）→ 屏显状态不在这条通道里；写 `c19` bit5 又被主板丢弃；
  疑似管屏显的 4 个字（`4d04 / 4d22 / 4d23 / 4d24`，都是"回 STATUS 但块不动"）也逐一试过，
  屏幕毫无反应。所以要读/控屏显只能走云端或模块级未知命令，不值得为它投入。
- 没接的：UDP 7083 自动发现（一代模块的应答里没有可靠的端口 TLV），所以填 IP 是手动的。
- 没接的：**用电量 / 实时功率**。本地 EPP 方言里没有这个字段 —— 用
  `tools/explore/haier_energy_probe.py` 在真机上验过：`4dfe`（hOn 大数据帧，功率就在里头）
  和 `4c01`（按参数 ID 查）都被回 `INVALID`；全扫 `4d00~4dff` 只有 11 个字回**标准 24 字节状态块**
  （没有更长载荷）；模块级帧 `0x61 / 0x70 / 0x7c / 0xf7 / 0xfc` 全 `INVALID`；
  块里的未知字节也从不承载数据（只有 `c1` 室温、`c3` 湿度、`c5` 交换计数会动）。
  → 这台（gen-1 eSDK + smartair2 方言）**只把电量发给了云端**。想拿数据只有三条路：
  ① **外部计量**（16A 计量插座 / 培正 PZEM-004T / Shelly EM，纯本地、精度高，推荐）；
  ② 逆向智家云接口（能看到和 App 一样的数，但走云、要账号、有合规风险）；
  ③ 用运行时长 × 额定功率估算（零成本，误差 ±30%，只看趋势）。
- 意外发现（**已验证，但别乱试**）：`4d08 / 4d09` = 健康 关/开，`4d18 / 4d19` = 遥控锁 关/开，
  `4d1e / 4d1f` = `c19` bit0 关/开 —— 偶数字关、奇数字开、不带载荷；`4d04 / 4d22 / 4d23 / 4d24`
  也会应答但控制块看不出变化。这些字**会真改设置**（全扫时遥控锁就被置上了），
  要试先记基线块、动一项立刻核对并还原；`tools/explore/haier_field_probe.py` 就是干这个的。
  想靠它们补回显示屏 / 极速 / 静音：**没有对应的字**，此路不通（已实测）。
- 不碰云端：整条链路只有 LAN 内的明文 TCP。

## 12. 许可与免责

本项目以 **MIT 许可证**发布，全文见 [`LICENSE`](LICENSE)。

- 与海尔（Haier）**无任何关联**，未获其授权或背书；"Haier / 海尔" 等商标仅用于说明互操作对象。
- 只在你自己的局域网内跟你自己的设备通信；不涉及账号、密钥或云端接口，也不规避任何技术保护措施。
- 文档里公开了部分**会真实改动设备设置**的命令字（见 §9、§11）—— 照着做可能改坏设备，
  **风险自负**。软件按「原样」提供，不含任何担保。

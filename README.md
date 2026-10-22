# Oracle 免费 ARM 实例自动申请（macOS）

在 Oracle Cloud **Always Free** 额度内自动抢一台 ARM 实例
（`VM.Standard.A1.Flex`，默认 **2 OCPU / 12GB 内存 / 50GB 启动盘 / 必须公网 IPv4**），
**默认每 5 分钟抢一轮、持续抢到为止**，拿到公网 IPv4 后自动停止定时任务。月费 ¥0。

A1 免费实例长期「Out of host capacity」，手动刷网页既费时又抢不到。这个工具把它变成
一次配置、长期无人值守：配好 OCI API Key，双击一次安装，之后不用再登录 Oracle 控制台。

- 只依赖 **Python + 官方 OCI SDK + macOS launchd**，无 GUI、无 Docker、无数据库
- 免费额度**实时查询**（Limits API），不硬编码任何数字，绝不申请收费资源
- 幂等创建 + 超时对账 + 公网 IP 验证，**不会重复开机器**，也不会谎报成功
- 可选 Telegram 电报通知，抢到后手机第一时间知道

> 参考了 [alexpua/oci-arm-catcher](https://github.com/alexpua/oci-arm-catcher) 的错误分类与重试思路，
> 代码为独立实现（Python + OCI SDK，而非 OCI CLI 循环脚本），并额外做了免费额度实时校验、
> 幂等创建、超时对账和公网 IP 验证。

---

## 目录

- [1. 目标实例](#1-目标实例写死在代码里配置无法放大)
- [2. 申请节奏](#2-申请节奏两种模式schedule_mode-切换)
- [3. 可配置项](#3-可配置项oracle-armconfigjson)
- [4. 安装与使用](#4-安装与使用三个入口)
- [5. 一轮申请到底做了什么](#5-一轮申请到底做了什么)
- [6. 免费额度怎么校验](#6-免费额度怎么校验不硬编码任何数字)
- [7. 安全边界](#7-安全边界)
- [8. 文件与路径](#8-文件与路径)
- [9. 测试](#9-测试)
- [10. 常见问题](#10-常见问题)
- [捐赠支持](#捐赠支持)
- [免责声明](#免责声明)

---

## 1. 目标实例（写死在代码里，配置无法放大）

| 项目 | 值 |
|---|---|
| 区域 | `ap-singapore-1`（会校验账号 Home Region，不一致直接停止） |
| 规格 | `VM.Standard.A1.Flex` |
| OCPU | **2**（固定，见下方说明） |
| 内存 | **12 GB**（固定，见下方说明） |
| 启动盘 | 50 GB |
| 系统 | Ubuntu ARM64（aarch64；优先 LTS 且**非 Minimal** 版） |
| 网络 | Public Subnet + Internet Gateway |
| 公网 IPv4 | **必须分配并验证**，拿不到就停止重复申请并提示修复 |
| IPv6 | 默认关闭；开启后若子网无 IPv6 CIDR 会自动降级为只申请 IPv4 |

`Config` 里**没有** `ocpus` / `memory_gb` / `shape` 这类字段，规格只存在于
`oracle_arm/config.py` 的常量里，不存在被配置误改大（例如 4 OCPU / 24GB）的可能
（有回归测试守着，见 `tests/test_config.py`）。

> **关于 2 OCPU / 12GB**：Oracle 对 A1 的免费额度前后调整过，常见账号的上限是
> 2 OCPU / 12GB（Limits API 返回 `standard-a1-core-count` 上限 2、
> `standard-a1-memory-count` 上限 12）。本工具**按满额度申请**，一次把能拿的都拿到。
>
> 若你的账号额度更小，工具会在预检时直接停止并提示，**不会去申请收费资源**。
> 想改规格，只改 `oracle_arm/config.py` 的 `TARGET_OCPUS` / `TARGET_MEMORY_GB`
> 两个常量即可（`tests/test_config.py` 会钉住当前值，改常量要同步改那条断言）。
>
> 实测感受：新加坡 `AD-1` 的 A1 容量长期紧张，命中与否主要取决于供给，
> 而不是请求大小——同一账号用 2/12 与 1/6 都曾连续数百轮全败。所以默认直接按满额度抢。

---

## 2. 申请节奏（两种模式，`schedule_mode` 切换）

### 默认：`interval` —— 每 5 分钟一轮，持续抢

```
每 300 秒跑一轮：预检 → 逐个 AD 各试一次 → 结束 → 300 秒后再来
```

- 由 launchd 的 **`StartInterval: 300`** 触发（不是每天固定时刻）。
- 参考项目 [alexpua/oci-arm-catcher](https://github.com/alexpua/oci-arm-catcher) 就是这种模式：
  它的 `while true` 循环 + `RETRY_INTERVAL=300` 持续重试，README 里写得很直白——
  「capacity frees up unpredictably, in small windows, and whoever's polling at that
  second wins」。本工具用同样的节奏，但由 launchd 托管，不需要手动 `nohup` / `tmux`。
- 每轮会**依次尝试该区域的每个 AD 各一次**。新加坡实测只有 1 个 AD，
  所以实际是 **12 次创建请求/小时**，与参考项目持平。多 AD 区域（如东京、法兰克福）
  会自动变成 `12 × AD 数` 次，无需改配置。
- 成功拿到公网 IPv4 后立刻停止任务，不会一直打下去。

### 可选：`fixed` —— 每天固定几个北京时间时刻各跑一轮

```
02:05   04:55   08:05   20:45
```

- 由 launchd 的 `StartCalendarInterval` 触发，写入 plist 时会把北京时间**换算成本机时区**，
  所以 Mac 时区不是 UTC+8 也能在正确的北京时间触发。
- 请求量小得多（每天 4 轮），但命中概率也低得多。

### 怎么切换

编辑 `~/.oracle-arm/config.json`，再双击 `安装.command` 重装：

```jsonc
// 持续抢（默认）
{ "schedule_mode": "interval", "interval_seconds": 300 }

// 每天 4 个固定时刻
{ "schedule_mode": "fixed", "schedule_beijing": ["02:05", "04:55", "08:05", "20:45"] }
```

> 两种模式都受同一套保护：容量不足就结束本轮等下次、429 按 `Retry-After` 退避
> （单次上限 300 秒）、单轮最多退避重试 2 次、运行互斥锁保证两轮不重叠。
> 间隔最小 60 秒（`normalize()` 会夹住），再密就不礼貌了且容易吃 429。

---

## 3. 可配置项（`~/.oracle-arm/config.json`）

改完重新双击 `安装.command` 生效（后台任务跑的是 `~/.oracle-arm/app` 里的源码副本）。

| 键 | 默认值 | 说明 |
|---|---|---|
| `schedule_mode` | `"interval"` | `interval` = 每 N 秒一轮持续抢；`fixed` = 每天固定时刻 |
| `interval_seconds` | `300` | `interval` 模式的间隔秒数（60 ~ 86400，默认 5 分钟） |
| `schedule_beijing` | `["02:05","04:55","08:05","20:45"]` | **仅 `fixed` 模式使用**，严格 `HH:MM` |
| `display_name` | `arm-free-1` | 实例显示名（也会派生出 hostname） |
| `compartment_id` | 空 | 空 = 用 tenancy 根 Compartment |
| `oci_config_file` / `oci_profile` | `~/.oci/config` / `DEFAULT` | API Key 配置位置 |
| `ssh_public_key` | `~/.ssh/oracle_arm_ed25519.pub` | 登录新实例用的公钥（安装时自动生成） |
| `availability_domain` | 空 | 空 = 自动选择并轮换全部 AD |
| `subnet_id` | 空 | 空 = 自动查找可分配公网 IP 的 Public Subnet；填了也只查这一个 |
| `image_id` | 空 | 空 = 自动选最新 Ubuntu ARM64（优先 LTS） |
| `enable_ipv6` | `false` | 开启后若子网没有 IPv6 CIDR 会自动降级为只申请 IPv4 |
| `allow_unverified_quota` | `false` | 查不到免费额度时是否放行；默认 false（不确定就停止） |
| `notification` | `true` | 是否发 macOS 系统通知 |
| `telegram_bot_token` | 空 | 电报机器人 token；与 `telegram_chat_id` 都填了才启用 |
| `telegram_chat_id` | 空 | 消息发往哪（群填 `-100…`，私聊填你的 user id） |
| `telegram_message_thread_id` | `0` | 群话题 ID，0 = 普通消息 |
| `telegram_proxy` | 空 | 访问 Telegram 需要代理时填（如 `http://127.0.0.1:7890`），留空 = 直连 |
| `rate_limit_max_retries` | `2` | 单轮内 429 的退避重试次数（单次退避上限 300 秒） |
| `launch_timeout_seconds` | `900` | 等待实例进入 RUNNING 的上限 |

> 规格（`VM.Standard.A1.Flex` / 2 OCPU / 12GB / 50GB）与区域（`ap-singapore-1`）
> **不是配置项**，写死在代码里，无法被放大。

---

## 4. 安装与使用（三个入口）

需要 **macOS 11+**、**Python 3.9+**，以及一个能调 OCI API 的 API Key（见下）。

提供两种打开方式，**任选一种**：

| 方式 | 怎么用 | 说明 |
|---|---|---|
| **`.app` 双击** | 双击 `安装.app` / `状态.app` / `卸载.app` | 最省事。优先用 Warp 打开，没装 Warp 就退回 Terminal |
| **`.command`** | 在终端里跑 `bash 安装.command`（或 `open -a Warp 安装.command`） | 逻辑本体在这里；`.app` 只是薄包装 |

> 少数机器上双击 `.command` 会打开一个空白窗口（`Terminal.app` 的 `login` 起不来，
> 与脚本无关）。这时用 `.app` 双击入口，或先在终端里手动 `bash 安装.command`。

### 4.1 首次授权：准备 OCI API Key（只做一次）

1. 登录 OCI 控制台 → 右上角头像 → **My profile** → **API keys** → **Add API key**。
2. 选 **Generate API key pair**，下载私钥 `.pem`，把公钥内容粘贴进控制台。
3. 控制台会显示 **Configuration file preview**，把它保存为 `~/.oci/config`：

   ```ini
   [DEFAULT]
   user=ocid1.user.oc1..xxxx
   fingerprint=xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx
   tenancy=ocid1.tenancy.oc1..xxxx
   region=ap-singapore-1
   key_file=~/.oci/oci_api_key.pem
   ```

4. 私钥必须是 **PEM 格式**（文件里有 `BEGIN PRIVATE KEY` 或 `BEGIN RSA PRIVATE KEY`），
   并把权限设为 `chmod 600 ~/.oci/oci_api_key.pem`。

> 本工具**不保存 Oracle 网页登录密码**，只用 API Key 签名调用。私钥内容永远不会写进日志。

### 4.2 前置资源：一个能分配公网 IP 的 Public Subnet

在 OCI 控制台用 **Create VCN with Internet Connectivity** 向导建一次即可（一键完成
VCN + Internet Gateway + Public Subnet + 路由表）。本工具**不会自动创建任何网络资源**，
找不到合适的子网时会停下并在终端告诉你该怎么做。

### 4.3 双击 `安装.command`

安装脚本会自动完成：

1. 找到 Python 3.9+ → 在 `~/.oracle-arm/venv` 建虚拟环境；
2. `pip install oci`（直连失败时自动改走代理，可用 `ORACLE_ARM_PIP_PROXY` 指定）；
3. 生成默认配置、按需生成 SSH 密钥对（`~/.ssh/oracle_arm_ed25519`，只用于登录新实例）；
4. 把源码复制到 `~/.oracle-arm/app`，并写入/加载 launchd 定时任务
   （**可重复执行，永远只有一个任务**；后台任务从副本运行，不依赖项目目录，
   项目放在 `~/Downloads` 也不会被 macOS 的 TCC 限制挡住，移动项目也不影响定时任务）；
5. 跑一次**预检（dry-run，不会创建实例）**，把 Compartment / AD / 镜像 / 子网 / 免费额度结果打印出来。

预检通过 → 安装完成，之后无需人工干预。
预检不通过 → 定时任务会被自动暂停，按提示修好后在 `状态.command` 里恢复。

### 4.4 双击 `状态.command`

菜单式操作（每轮先打印一次完整状态）：

```
 1) 立即尝试一次（前台显示过程）   4) 恢复自动申请
 2) 只做预检（不创建实例）         5) 查看日志（最近 50 行）
 3) 暂停自动申请                   6) 环境与配置体检      0) 退出
```

状态里会显示：定时任务是否加载、下次申请时间与倒计时、上次结果、累计尝试轮数、
容量不足次数、Limits API 查到的额度、账号套餐、实例 OCID 与公网 IPv4、最近日志。

### 4.5 双击 `卸载.command`

只卸载本机 launchd 定时任务；**不删除、不停机任何云端实例**，配置与日志也保留。

---

## 5. 一轮申请到底做了什么

```
run（launchd 触发：StartInterval 每 300 秒 / 或 StartCalendarInterval 固定时刻）
├─ 取互斥锁 ~/.oracle-arm/run.lock：上一轮还在跑就本轮跳过（绝不并发创建实例）
├─ 配置校验（区域必须新加坡、时刻格式、SSH 公钥存在）
├─ 认证：读取 ~/.oci/config，构造 OCI 客户端（失败 → 暂停并提示怎么重新生成 API Key）
├─ 预检
│   ├─ Compartment 存在且 ACTIVE（默认用 tenancy 根 Compartment）
│   ├─ 账号 Home Region 必须是 ap-singapore-1，~/.oci/config 的 region 也必须一致
│   ├─ 列出现有实例：已有本工具的 A1 实例 → 直接确认状态，绝不重复创建
│   ├─ 逐个 AD 查 shape：billing_type 必须是 ALWAYS_FREE / LIMITED_FREE，否则停止
│   │    查询失败是「不知道」不是「不可用」：每个 AD 都只是查不到（网络抖动）→ 本轮跳过，
│   │    下个申请时刻重试，**不暂停定时任务**
│   ├─ 查免费额度（Limits API，见 §6）：上限不够 → 停止；该 AD 用满 → 换下一个 AD
│   │    会触发「停止」的读数一律隔 5 秒复查一次，两次一致才照办
│   ├─ 找 Ubuntu ARM64 镜像（优先 LTS）与可分配公网 IP 的 Public Subnet
│   └─ 以上任一项缺失 → 暂停定时任务 + 系统通知 + 终端说明怎么修
├─ 创建（规格固定 2 OCPU / 12GB / 50GB，assign_public_ip=True）
│   ├─ 每个 AD 一次逻辑请求，带 opc_retry_token 幂等标识
│   ├─ 容量不足（Out of host capacity）→ 换下一个 AD；全部不行 → 本轮结束，等下次
│   ├─ 429 → 按 Retry-After 退避后重试（最多 2 次，单次上限 300 秒），用尽则本轮放弃
│   ├─ 超时/网络错误 → **先查询实例状态对账**；确认已创建就直接收尾（**绝不再发创建请求**），
│   │                 未查到才用同一幂等标识重试一次，仍失败则本轮放弃（不盲目重试）
│   └─ 认证/配额/配置错误 → 暂停定时任务并说明原因
└─ 成功收尾
    ├─ 等待实例 RUNNING（上限 900 秒；未完成则本轮结束，下次继续确认，不重复创建）
    ├─ 验证公网 IPv4 真的分配到了（最多轮询约 110 秒）
    │   ├─ 拿到 → 记录 OCID + IP、发通知、**停止定时任务**
    │   ├─ 查询成功但确实没 IP → 状态置「需人工处理」、停止定时任务、提示去控制台修复
    │   └─ 查询本身全部失败 → 本轮不下结论（下个申请时刻继续确认），不误报、不暂停
    └─ 写日志（~/.oracle-arm/logs/oracle-arm.log，2MB 滚动切割）
       容量不足时每轮只记一行 INFO 汇总，但汇总里带官方原文（例如
       「1 个 AD 均容量不足（累计 787 次；最近一次：容量不足：Out of host capacity.）」），
       事后一眼能分辨是「真没容量」还是「被限流/被拒」——单条失败仍记 DEBUG 免得刷满日志
```

### 成功后为什么不会继续申请（四层防护）

「申请成功一个就停」不是单点判断，任何一层单独失效都还有下一层兜住：

| 层 | 位置 | 作用 |
|---|---|---|
| 1 | `runner._mark_success` | 拿到公网 IPv4 后立即 `launchd.pause`（`disable` + `bootout`），定时任务本身不再触发 |
| 2 | `cli.cmd_run` 入口 | 若 `state.status == success`，**连 OCI 都不调用**就直接返回（`--force` 可显式绕过） |
| 3 | `runner._check_existing_instance` 预检 | 每轮先列出实例；发现本工具标签（`managed-by=oracle-arm`）的 A1 实例就只确认、**绝不发创建请求** |
| 4 | `opc_retry_token` 幂等 | 同一次逻辑请求重复提交也不会创建出第二个实例 |

第 3 层是最后的保险：即使用户手动 `resume` 或用 `--force` 跑了一轮，也只会确认已有实例，
不会再多开一台。如果账号里已有**非本工具创建**的 A1 实例，则直接停下并提示，
不会把它误报成「申请成功」，也不会另建一台。

### 抢到后的通知（macOS + Telegram）

成功时会发两种通知：macOS 系统通知（本机弹窗）与 **Telegram 电报消息**（手机随时能看到）。

电报是可选功能，`telegram_bot_token` 与 `telegram_chat_id` 都填了才启用；缺一个就静默跳过，
只发 macOS 通知。凭据只存 `~/.oracle-arm/config.json`（权限 600），**不写进代码、不进 git**。

配置方式：在 Telegram 里找 [@BotFather](https://t.me/BotFather) 建一个 bot 拿 token；
把 bot 拉进要收消息的群（或直接私聊它并 `/start`），`chat_id` 填群 ID 或你的 user ID。
访问 `api.telegram.org` 需要代理时，把 `telegram_proxy` 填上。

> **抢到实例只发生一次**，等到那一刻才发现电报发不出去就晚了。所以配好之后先手动验证：
>
> ```bash
> ./.venv/bin/python -m oracle_arm telegram-test
> ```
>
> 收到消息说明 token / chat_id / 代理都对。

几个实现上的取舍：

- **只用标准库 urllib**：项目唯一的第三方依赖是 OCI SDK，为一条通知引入 requests 不划算。
- **不用 Markdown/HTML**：实例名与 IP 里可能出现 `_` `-` `.`，Telegram 解析失败会直接拒收
  （400 `can't parse entities`），纯文本最稳。
- **失败绝不影响主流程**：电报发不出去最多丢一条消息，`state.status=success`、
  停止定时任务这些结论必须照常落盘（有测试守着）。
- **只在「刚抢到」时发**：`_mark_success` 有两个调用方——本轮真的创建成功，以及预检发现
  实例早已存在（手动 `resume` 后再跑一轮）。只有前者才算刚抢到，否则每次手动跑都会
  重复发一条，变成骚扰。

---

## 6. 免费额度怎么校验（不硬编码任何数字）

官方文档与社区资料对 Always Free 额度的描述不一致（A1 曾从 4 OCPU / 24GB 调整过），
所以本工具**不写死上限**，而是运行时用官方接口实时查询：

1. `list_shapes` 取 `VM.Standard.A1.Flex` 的 **`billing_type`**，必须是 `ALWAYS_FREE` **或
   `LIMITED_FREE`**（A1 实测是 `LIMITED_FREE` = 额度内免费、超出才计费；
   `VM.Standard.E2.1.Micro` 才是 `ALWAYS_FREE`）。`PAID` / 未知值 → 停止；
2. 取该 shape 的 **`quota_names`**，逐个调用 **Limits API `get_resource_availability`**，
   得到 A1 核心/内存的**已用量、剩余量、额度上限**。两个实测坑：
   - `quota_names` 里 AD 级与 REGION 级混在一起，**区域级额度不能传 `availability_domain`**
     （否则 400 `Parameter 'availabilityDomain' should be null`），代码按 `scope_type` 区分；
   - `effective_quota_value` 可能返回 `None`，此时用 `已用 + 可用` 作为该额度的上限
     （当成 0 会误判成「额度不足」直接停摆）；
3. 判定规则：
   - 上限本身 < 2 OCPU 或 < 12GB → **直接停止**，提示额度不足，绝不申请收费资源；
   - 某个 AD 剩余量不足 → 换下一个 AD 继续；
   - **查不到额度**（无 limits 读权限等）→ 默认**停止**（不确定是否免费就停）。
     确需放行可在配置里设 `allow_unverified_quota: true`，自行承担风险。
4. **「额度不足」的结论必须复查确认**。Limits API 是最终一致的，实测出现过**瞬时假读数**：
   某一刻读到「已用 2/2、可用 0」，几秒后复查即恢复 0/2，OCI 审计日志也证实那段时间
   **没有任何实例创建/终止事件**。只凭一次读数就把定时任务永久停掉，对抢容量工具是最糟的
   失败模式（静默停摆，而抢容量分秒必争），所以凡是**会触发「停止」的读数**（上限不够、
   或该 AD 占满）都会隔 `QUOTA_RECHECK_WAIT`（5 秒）复查一次。判定规则：

   | 两次读数 | 处置 |
   |---|---|
   | 都判「额度充足」 | 照常申请（正常路径只读一次，不复查） |
   | 都判「账号上限不够」 | 停止并提示（绝不申请收费资源） |
   | 都判「该 AD 占满」 | 换下一个 AD；全部占满才提示人工 |
   | 一次判「上限不够」、一次判「占满」 | **结论不一致 → 本轮跳过**：不申请、也不停表 |
   | 复查本身读不到 | 首次已判负，**不申请**（绝不把「不知道」当「够」），本轮跳过 |

   一句话：**「停止」和「申请」都要求两次读数同因一致；不一致就只跳过本轮**，下个申请
   时刻重新判，既不误停也不冒险。

状态页会把查到的额度原样显示（含 scope），例如：

```
A1 核心 已用 0/2（可用 2）；内存 已用 0/12 GB（可用 12），来源 AD
账号套餐：FREE_TIER
```

> 账号套餐用 OSP 订阅接口查询，**`osp_home_region` 必须传账号的 Home Region**
> （例如新加坡账号传 us-ashburn-1 会返回 Internal Server Error）。`FREE_TIER` 表示
> 账号本身无法产生费用；查询失败不影响放行判断（额度判定才是权威口径）。

---

## 7. 安全边界

- 不创建任何收费资源、不升级付费规格、**不删除或修改任何现有资源**。
- 不自动创建 VCN / 子网 / Internet Gateway（找不到就停下并给操作指引）。
- 不保存 Oracle 网页登录密码，只用 API Key。
- 私钥内容不进入日志（`util.redact()` 兜底过滤私钥块与 `password/secret/token=` 赋值）。
- `~/.oracle-arm/config.json` 权限 `600`；Telegram `bot_token` 只存在这个文件里，
  **不写进代码、不进 git**；日志与状态页都不打印 token（失败日志只记异常类型与 HTTP
  状态码，不记含 token 的 URL）。
- 幂等创建：`opc_retry_token` 保证超时重试不会创建出两个实例；超时后先查询状态再决定。
- 已有 A1 实例（无论是否本工具创建）都不会重复申请。
- 互斥锁 `~/.oracle-arm/run.lock` 保证两轮申请不会重叠；睡眠期间会持续刷新锁（心跳），
  超过 30 分钟无心跳的残留锁才自动接管。
- 429 的退避时长有上限（300 秒）：`Retry-After` 偶尔会给出 7200 这种值，原样照睡会错过
  后续申请时刻。
- 账号里已有**别人的** A1 实例时，既不重复创建，也**不会**谎报「本工具申请成功」，
  而是停在「需人工处理」并说明清楚。

---

## 8. 文件与路径

```
项目目录（可放任意位置）
├── 安装.command / 状态.command / 卸载.command   双击入口
├── 安装.app / 状态.app / 卸载.app               .app 薄包装
├── requirements.txt / requirements-dev.txt
├── assets/donate/         捐赠二维码
├── oracle_arm/            源码
│   ├── config.py          配置、路径、状态、目标规格（写死）
│   ├── client.py          OCI SDK 薄封装（可被测试整体替换）
│   ├── errors.py          异常分类：容量/429/认证/配额/配置/超时
│   ├── runner.py          单轮申请主流程
│   ├── launchd.py         plist 生成与加载/卸载/暂停
│   ├── util.py            日志、脱敏、macOS 通知、北京时间换算
│   ├── telegram.py        Telegram 通知（可选）
│   └── cli.py             命令行入口
└── tests/                 单元测试（全部 Mock，不碰真实 OCI）

用户目录
├── ~/.oci/config                          你的 OCI API Key 配置（你自己创建）
└── ~/.oracle-arm/
    ├── config.json                        本工具配置（600）
    ├── state.json                         运行状态
    ├── venv/                              虚拟环境
    ├── app/oracle_arm/                    源码副本（后台任务实际运行的位置）
    └── logs/oracle-arm.log                申请日志
```

---

## 9. 测试

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m pytest tests/ -q
```

**结果：262 passed**（全部使用 Mock，不会创建任何真实实例，也不会发出真实网络请求）。

| 测试文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/test_runner.py` | 73 | 容量不足、429 退避与用尽、认证/配额/配置暂停、重复申请防护、超时对账、公网 IP 轮询、成功停止、IPv6 开关、dry-run、跨轮次行为、电报只发一次 |
| `tests/test_cli.py` | 49 | 安装/重复安装/卸载/暂停/恢复/状态/立即尝试/日志/源码副本/并发跳过/坏配置容错/电报测试命令 |
| `tests/test_config.py` | 37 | 配置读写与校验、规格不可放大、原子写、权限 600、状态机 |
| `tests/test_client.py` | 29 | **用真实 OCI SDK 模型**校验构造出的请求（固定规格、公网 IP、IPv6、幂等标识、显式 subnet_id 的网关判定） |
| `tests/test_errors.py` | 27 | 异常 → 处置动作的分类表（含 `Retry-After` 解析、容量正则不吞参数错误） |
| `tests/test_util.py` | 17 | 时区换算、脱敏、日志落盘、运行互斥锁与心跳 |
| `tests/test_launchd.py` | 15 | plist 内容、4 个时刻、RunAtLoad=false、幂等安装、暂停先 disable 后 bootout |
| `tests/test_telegram.py` | 11 | 电报开关门禁、请求体、话题 ID、代理、失败降级、**日志不泄露 token**、正文关键信息 |
| `tests/test_scripts.py` | 4 | 扫描全部 shell 脚本（按 shebang）：禁止 `$VAR` 紧邻中文标点 |

关键场景与对应测试：

| 场景 | 测试 |
|---|---|
| 容量不足等下一次（不暂停、不通知） | `test_capacity_shortage_waits_for_next_round` |
| 429 按 Retry-After 退避后成功 | `test_rate_limit_backs_off_then_succeeds` |
| 认证失败暂停并通知 | `test_human_errors_pause_and_notify` |
| 重复申请防护（已有实例） | `test_existing_managed_instance_with_ip_is_success` |
| 超时先对账再决定 | `test_timeout_reconciles_and_finds_created_instance` |
| 公网 IPv4 验证 / 成功停止定时任务 | `test_success_assigns_public_ip_and_stops_schedule` |
| 无公网 IP 停止重复申请 | `test_running_without_public_ip_stops_and_warns` |
| 请求正好等于账号上限时放行 | `test_quota_accepts_exactly_the_account_limit` |
| 两轮不重叠（防重复创建） | `test_run_skips_when_another_round_is_running` |
| 对账确认后绝不再发创建请求 | `test_reconcile_found_instance_never_retries` |
| 别人的 A1 实例不谎报成功 | `test_foreign_a1_instance_is_not_claimed_as_success` |
| 显式 subnet_id 也判定网关 | `test_explicit_subnet_id_resolves_internet_gateway` |
| IPv6 真的写进创建请求 | `test_launch_requests_ipv6_when_enabled_and_cidr_given` |
| 配置损坏不崩、且不带着默认值去创建 | `test_broken_config_does_not_crash_status` / `test_broken_config_stops_run_without_touching_oci` |
| 配置/状态是 `null`/`[]` 也不崩 | `test_non_object_config_does_not_crash_any_command` |
| 配置坏了不注册任务、不恢复任务 | `test_install_refuses_when_config_broken` / `test_resume_refuses_when_config_broken` |
| 同名外来实例不谎报成功 | `test_foreign_instance_with_default_name_is_not_claimed` |
| 403 不被误判成「实例已删除」 | `test_instance_403_is_not_treated_as_deleted` |
| A1 的 `LIMITED_FREE` 被正确放行 | `test_limited_free_shape_is_accepted` |
| 区域级额度不传 AD | `test_limit_availability_omits_ad_for_regional_scope` |
| `effective_quota_value` 为 None 时回退 | `test_limit_availability_falls_back_when_quota_value_is_none` |
| 镜像优先完整版而非 Minimal | `test_find_image_prefers_full_over_minimal` |
| 预检瞬时错误只跳过本轮 | `test_transient_error_in_preflight_skips_round` |
| 额度假读数不永久停表（复查恢复） | `test_transient_quota_reading_does_not_pause` |
| 额度复查仍满才停表（且确实复查过） | `test_quota_full_confirmed_by_recheck_still_pauses` |
| 两次读数结论不一致只跳轮不停表 | `test_disagreeing_quota_readings_do_not_pause` |
| 抢到后发一条电报（带规格与 IP） | `test_success_sends_telegram_once` |
| 重新确认已有实例不重复发电报 | `test_reconfirming_existing_instance_does_not_resend_telegram` |
| 电报发不出去也不影响「已成功」 | `test_telegram_failure_does_not_break_success` |
| 日志与状态页都不泄露 bot_token | `test_token_never_appears_in_log` / `test_status_shows_telegram_configured` |
| 容量不足汇总行带申请规格与官方原文 | `test_capacity_summary_includes_target_shape` / `test_capacity_summary_carries_raw_oci_message` |
| 复查失败不把「不知道」当「够」 | `test_unverified_recheck_after_negative_reading_does_not_launch` |
| 额度上限被假读成 0 也不停表 | `test_transient_zero_quota_cap_does_not_pause` |
| 所有 AD 规格查询失败只跳轮不暂停 | `test_transient_shape_lookup_on_every_ad_skips_round_without_pausing` |
| 额度查询超时只跳轮不暂停（403 仍停） | `test_transient_limits_query_failure_skips_round_without_pausing` |
| 已有确定结论时网络抖动不掩盖它 | `test_shape_transient_mixed_with_real_reason_still_pauses` |
| 未预期异常不裸崩 | `test_unexpected_exception_does_not_crash` |
| 变量名紧邻中文标点不报 unbound variable | `test_no_bare_var_followed_by_non_ascii` |

> 测试与安装预检都是 dry-run，不会真的创建 Oracle 实例。

---

## 10. 常见问题

**Q：一直显示「容量不足」怎么办？**
A：新加坡 A1 容量确实经常紧张，这是容量问题不是缺陷。默认模式下工具每 5 分钟自动重试一轮，
无需干预；`状态.command` 里能看到累计容量不足次数。想更密可以调小 `interval_seconds`
（下限 60 秒），但越密越容易吃 429。

**Q：为什么一直打接口，会不会被封？**
A：默认每 5 分钟一轮，和参考项目的默认值一致；遇到 429 会按官方 `Retry-After` 退避
（单次上限 300 秒）并最多重试 2 次，重试用尽就跳过本轮。不想这么频繁就把
`interval_seconds` 调大，或切回 `fixed` 模式。

**Q：状态突然变成「需人工处理」，说「该 AD 剩余额度不足」，但控制台看额度明明是 0/2？**
A：这是 Limits API 的**瞬时假读数**：那一刻它返回「已用 2/2、可用 0」，几秒后复查就恢复
0/2，OCI 审计日志也证实当时并没有任何实例存在。旧版本只读一次就下结论，于是把假读数
当成「额度耗尽」永久停掉了定时任务。现在凡是会触发「停止」的额度读数都会隔 5 秒复查一次，
两次一致才停。遇到旧版本留下的停表，双击 `状态.command` → 选 4「恢复自动申请」即可。

**Q：日志里只写「N 个 AD 均容量不足」，看不到官方到底返回了什么？**
A：单条创建失败记 DEBUG（高频模式下是常态，全记 INFO 会把日志刷满），但每轮的 INFO
汇总行会带上官方原文，例如
`1 个 AD 均容量不足（累计 787 次；最近一次：容量不足：Out of host capacity.），申请规格 2 OCPU / 12GB，等待下一个申请时刻`。
看到 `Out of host capacity.` 就是真的没容量；看到 429 / 认证 / 配额字样则是另一类问题。

**Q：网络抖了一下，定时任务会不会被停掉？**
A：不会。这类「瞬时信号 → 永久停表」的路径已全部修掉：
1. **所有 AD 的规格查询都失败**（例如一次读超时）以前会走到「没有可用的 Availability
   Domain」并停表。现在只跳过本轮、下个申请时刻重试。但若已有 AD 给出了确定结论
   （例如额度确实占满），其余 AD 的网络抖动不会掩盖它，仍会照常提示人工处理。
2. **额度查询本身超时/网络错误**（不是 403 权限问题）同样只跳过本轮；
   只有真正需要人工的错误（如 `NotAuthorized` 没有 limits 读权限）才停表。
3. **额度读数**见上一条（复查确认）。

判断标准统一成一句话：**只有「需要人工介入」的错误才停表，瞬时错误一律只跳过本轮**。

**Q：提示「无法查询账号免费额度」。**
A：说明当前 API Key 的用户没有 limits 读权限。用管理员用户，或在 IAM 里加
`Allow group X to read limits in tenancy`。

**Q：实例 RUNNING 但没公网 IPv4。**
A：定时任务会**自动停止**并提示。请检查该子网的路由表是否有 `0.0.0.0/0 → Internet Gateway`、
子网是否勾了「Prohibit public IP on VNIC」，然后手动分配公网 IP，再恢复任务。

**Q：安装时 pip 装不上。**
A：需要联网。直连失败时脚本会自动改用代理（默认 `http://127.0.0.1:7890`，即 Clash 默认端口）。
端口不同就先设环境变量再安装：

```bash
ORACLE_ARM_PIP_PROXY=http://127.0.0.1:1080 bash 安装.command
```

**Q：Mac 时区不是北京时间。**
A：不需要改。写入 plist 前会自动把北京时间换算成本机时区。

**Q：账号里已经有一台 A1 实例（不是本工具创建的），会怎样？**
A：工具**不会**重复创建，也**不会**把它谎报成「本工具申请成功」。它会停在
「需人工处理」并告诉你那台实例的名字和 OCID。如果那台就是要用的机器，什么都不用做；
如果要让本工具接管，把它的显示名改成 `arm-free-1`（或先删掉它），再恢复任务。

**Q：状态显示「需人工处理」，怎么恢复？**
A：按状态页里的「原因」处理后，双击 `状态.command` → 选 4「恢复自动申请」。
恢复会把状态重置为 idle，下一个申请时刻重新跑一轮预检。

**Q：暂停后重启 Mac 会自动恢复吗？**
A：不会。暂停用的是 `launchctl disable`（持久禁用，跨登录/重启有效）——只 `bootout` 是不够的，
因为 plist 还在 `~/Library/LaunchAgents`，下次登录 macOS 会自动把它加载回来。
恢复必须显式运行「状态.command」→ 恢复（内部会先 `enable` 再加载）。

**Q：双击 `安装.command` / `状态.command` 窗口是空的？**
A：这是 macOS `Terminal.app` 的 `login` 卡住导致的，与脚本无关（在终端里 `bash 状态.command`
能正常跑）。改用 `.app` 双击入口，或在已安装的 Warp / iTerm 里手动执行。

**Q：我改了项目里的代码，后台任务会跟着变吗？**
A：不会，需要重新双击 `安装.command`。后台任务跑的是 `~/.oracle-arm/app` 里的源码副本
（这样后台任务不依赖项目目录，项目放在 `~/Downloads` 也不会被 macOS 的 TCC 限制挡住）。
重装会把最新源码重新同步过去。

**Q：怎么确认定时任务真的注册了？**
A：`launchctl print gui/$(id -u)/com.oracle-arm.hunter | head -30`，
或双击 `状态.command` 看第一行「定时任务」。

**Q：安装脚本报 `PYTHON（: unbound variable`？**
A：已在 v1.0 修复。根因是脚本里写成 `"$PYTHON（"`——变量名紧挨全角括号，
macOS 自带 bash 3.2 在 UTF-8 locale 下会把「（」的首字节算进变量名，变量于是变成不存在的
`PYTHON（`，配上脚本开头的 `set -u` 直接报错；C locale 下反而正常，所以看着像「重启后坏了」。
现在全部改成 `"${PYTHON}（"`，并有 `tests/test_scripts.py` 扫描所有 shell 脚本防回归。

---

## 捐赠支持

本工具永久免费开源。如果它帮你抢到了那台一直抢不到的免费 ARM 实例，
欢迎请作者喝杯咖啡 ☕

| 方式 | 链接 / 二维码 |
|---|---|
| ⚡ 爱发电 | [afdian.com/a/eddiezhan](https://afdian.com/a/eddiezhan) |
| 💚 微信赞赏 | ![微信](./assets/donate/wechat_qr.png) |
| 💙 支付宝 | ![支付宝](./assets/donate/alipay_qr.png) |
| ₿ USDT (TRC20) | ![USDT](./assets/donate/usdt_qr.png) |

> 捐赠完全自愿。不捐也照样能用全部功能，也不会影响后续更新。

---

## 免责声明

本项目仅供个人学习与技术交流使用，按「现状」提供，不对使用后果作任何担保。

- 本工具只调用 Oracle 官方公开 API，请在遵守
  [Oracle Cloud 服务条款](https://www.oracle.com/cloud/sla/)的前提下使用；
  因使用本工具导致的账号限制、实例回收或其他后果，由使用者自行承担。
- 免费额度政策由 Oracle 决定并可能随时调整。本工具会实时校验额度以避免产生费用，
  但**不构成任何费用保证**，请自行在控制台确认账单。
- 请勿把本项目用于批量注册、薅取资源或任何违反服务条款的用途。

## License

[MIT](./LICENSE)

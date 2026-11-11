# Profile 契约与 TCL / Media 隔离

## 1. 一个 profile = 一台实测台架

profile 是完整的现场描述，**没有任何继承机制**。加载器拒绝 `inherits`、`extends`、
`defaults_from`、`parent_profile`、`$ref`、`include` 这些键，哪怕值是 `false`。

```json
{
  "schema": "venusahisiburner-profile/v1",
  "name": "tcl-pvm1005",
  "node": "pvm1005",
  "board_family": "tcl",
  "board": "TCL-CSK7-HI3863",
  "status": "measured",
  "time_contract": "beijing-no-offset",
  "control": { "...": "见第 2 节" },
  "chips": [ { "...": "见第 3 节" } ]
}
```

| 字段 | 规则 |
|---|---|
| `board_family` | `tcl` / `midea` / `generic` |
| `status` | `measured` 才允许写阶段；`draft` 必须给 `measurement_required` |
| `time_contract` | 只接受 `beijing-no-offset` |
| `preflight_window_s` | preflight 读串口窗口，默认 6 s |

## 2. control 块

`control.kind`：

- `serial-relay`：本机控制串口发命令。必须有 `port`（COMx）、`baud`、`commands.power_cut`、
  `commands.power_restore`、`receipt_tokens`（按命令前缀匹配回执 token）和 `timing`。
  `commands` 只允许 `power_cut` / `power_restore` / `strap_on` / `strap_off`。
- `external-script`：项目自己的驱动脚本（Media 用 `auto_burn.py`），profile 只声明 `script`。
- `manual`：人工断电，写阶段由人确认（本 Skill 不自动执行）。

`timing`（measured 的 serial-relay 必填，单位秒）：`cut_to_strap_s`、`strap_to_restore_s`、
`restore_to_strap_off_s`、`restore_to_verify_s`、`cycle_hold_s`。

## 3. chips 块

数组顺序就是执行顺序。每个 chip：

| 字段 | 说明 |
|---|---|
| `id` | 步骤名后缀，小写，如 `venusa` / `hisi` / `csk6` |
| `chip` | 目标芯片描述（`CSK7 / VenusA`、`HI3863 / WS63`） |
| `boot.required` + `boot.method` | 需要 boot strap 时用 `control-strap` |
| `transport` | `log_port` / `log_baud` / `burn_port` / `burn_baud` |
| `burn.driver` | `venusa-uart` 或 `hisi-burntool`（已实现）；`csk6-batch` 未实测，measured profile 直接拒绝 |
| `burn.tool` / `burn.image` | 资产 id，指向 `assets/manifest.json`，不是路径 |
| `verify` | `version_key`、`require_keys`、`expect`、`window_s`、可选 `cross_check` |
| `recover` | `required`、`driver`（`ftm-clear` / `power-cycle` / `none`）、`before_verify` |

驱动细节：

- `venusa-uart`：`Uart_Burn_Tool.exe -b <burn_baud> -p <burn_port> -f <image> [-l -m -d -s] -a <image_addr>`，
  `image_addr` 必须是 `0x...`；成功标记来自工具输出（`MD5 CHECK SUCCESS`、`FLASH DOWNLOAD SUCCESS`）。
- `hisi-burntool`：`BurnTool.exe -com:<n> -bin:<file> -signalbaud:<baud> -chiptype:<id> -onlyburn:<part>...`，
  成功/失败词来自 optLog（默认 `成功`/`完成`、`失败`）。

## 4. 族隔离规则（加载时强制）

1. `board_family: tcl` 的 `control.forbidden_commands` 必须包含 `uut-reset`、`uut-csk-reset`、`uut-burn`；
   `control.commands` 里出现任何被禁命令即拒绝。
2. 非 tcl 族只要出现 `uut-*` 命令就拒绝，除非 profile **显式**写 `control.allow_tcl_commands: true`——
   这是“自己声明”，不是从 TCL 继承。
3. `boot.required` 的 chip 必须能在本 profile 的 `commands` 里找到 `strap_on`/`strap_off`。
4. 没有默认值：缺 `port`、`baud`、`expect` 等字段就报错，不会回落到任何其他 profile 的值。

## 5. TCL 与 Media（美的）差异

| 维度 | TCL (`tcl-pvm1005`) | Media (`midea-csk6`, draft) |
|---|---|---|
| 控制方式 | 本机控制串口 `COM6`，`uut-switch1.on/off` + `uut-csk-boot.on/off` | 项目自己的 `auto_burn.py`（`external-script`），不声明本机控制命令 |
| 目标芯片 | CSK7 + HI3863 双芯片 | CSK6（`csk6-batch`，未实测） |
| 串口/波特率 | COM3@921600 / COM4@921600，BurnTool `-com:4` | 全部为 `null`，待现场测量 |
| 烧录工具 | `Uart_Burn_Tool 1.0.10` + `BurnTool_Gold` | `CSK6_Burn.bat`（本 Skill 不内置驱动） |
| 工厂模式恢复 | `AT+FTM=0` @115200，且必须在 921600 校验前 | 不适用，禁止继承 TCL 的 FTM 时序 |
| 断电/上电时序 | `cut 2s → strap → 1s → restore → 8s → strap off` | 由 Media 脚本自行控制 |
| 状态 | measured（2026-11-10 回归 PASS） | draft，写阶段被拒绝 |

### Media 独立验收项（与 TCL 回归分开）

1. 在 Media 台架测出控制脚本路径、log/burn 端口与波特率、断电/恢复方式。
2. 把 CSK6 工具与镜像登记进 `assets/manifest.json`（含 SHA-256），再改 profile 引用。
3. 实现并单测 `csk6-batch` 驱动后，才能把 `status` 改为 `measured`。
4. 独立跑一次 Media 基线回归并生成自己的 receipt；不得复用 TCL 的 receipt 或归档。
5. 差异复核：Media profile 中不得出现 `uut-switch1`、`uut-csk-boot`、`COM6`、`AT+FTM=0` 等 TCL 值
   （`tests/test_profile.py::MediaIsolationTests` 守着）。

## 6. 新增一台台架的最小流程

1. 现场测端口、波特率、断电/恢复方式、启动版本串。
2. 复制最接近的 profile，改 `name`/`node`/`board_family`，`status` 先写 `draft`。
3. 把工具与固件登记到 `assets/manifest.json`（sha256 必须是实测值）。
4. `plan` + `preflight` 跑通后，补全期望版本，改 `status: measured`。
5. `all --confirm-write` 回归，证据进任务目录，receipt PASS 才算该台架可用。

## 7. 设备端口来源：`--device-snapshot`

主入口 `scripts/burn.py` 增加可选参数 `--device-snapshot FILE`，五个子命令都能用。
给了它就以文档为准，覆盖 profile 的 `transport.log_port` / `transport.burn_port` 与
`control.port`；**没给时行为与旧版完全一致**（端口仍取 profile）。

### 7.1 接受的文档

| schema | 来源 | 消费方式 |
|---|---|---|
| `ark.device-io.v1` | `device-io ports --format json --out FILE` | 原始快照，按 profile 已声明的端口名/interface 标签匹配 |
| `ark.device-role-map.v1` | `device-io roles --spec spec.json --format json --save FILE` | 直接取 `roles[role].port` |

按顶层 `schema` 分派；其他 schema 直接失败。解析只读 JSON，本 Skill 不枚举 USB/串口。

### 7.2 role 命名

| role | 覆盖的 profile 字段 |
|---|---|
| `<chip>:log` | `chips[].transport.log_port` |
| `<chip>:burn` | `chips[].transport.burn_port` |
| `control` | `control.port`（仅当 profile 声明了控制口） |

双芯片（`tcl-pvm1005`）用 `venusa:log`、`venusa:burn`、`hisi:log`、`hisi:burn`、`control`。
单芯片 profile（如 `midea-csk6`）额外接受裸 `log` / `burn`。双芯片 profile 里出现裸
`log` / `burn` 会硬失败并提示改用 `<chip>:log` / `<chip>:burn`。

### 7.3 role-map 解析规则

```json
{
  "schema": "ark.device-role-map.v1",
  "generated_at": "2026-11-11 18:00:00",
  "spec_schema": "ark.device-roles-spec.v1",
  "ok": true,
  "roles": {
    "venusa:log": {"state": "resolved", "port": "COM13", "reason": "matched interface SERIAL-A"},
    "venusa:burn": {"state": "resolved", "port": "COM13", "reason": "matched interface SERIAL-A"},
    "hisi:log": {"state": "resolved", "port": "COM15", "reason": "matched interface SERIAL-B"},
    "hisi:burn": {"state": "resolved", "port": "COM18", "reason": "matched interface SERIAL-C"},
    "control": {"state": "pinned", "port": "COM16", "reason": "caller pinned COM16"}
  }
}
```

- `state` 为 `resolved` / `pinned` 且 `port` 非空 → 采用该端口。
- `state` 为 `ambiguous` / `missing` / `conflict`，或 role 缺失，或顶层 `ok: false`
  → 硬失败，报错带 role、state、`reason`。
- 同芯片的 log 与 burn 允许是同一个物理口（TCL 的 VenusA 就是 COM3 同时做打印与烧录），
  端口重复本身不算错误；是否冲突由 device-io 的 `state` 判定。

### 7.4 原始快照解析规则

- 只用 profile 里**已经写下的**端口名（`COM6`、`/dev/ttyUSB0`）或 interface 标签，
  与 `ports[].device` / `ports[].interface` 做**大小写不敏感的精确相等**比较。
- `present: false` 的端口不参与匹配。
- BurnTool 的 `-com:<n>` 数字形式与 `COM<n>` 是同一个设备：profile 的 `burn_port` 写 `4`
  时按 `COM4` 匹配（只做这一种等价，不做前缀/序列号/模糊猜测）。
- 0 命中 → `missing`；多于 1 个不同 device 命中 → `ambiguous`；恰好 1 个 → 采用。
- profile 里该 role 没有声明端口（如 draft profile 的 `null`）→ 硬失败并提示改用 role-map。

### 7.5 输出与失败

`plan`（含 `--dry-run`）在文本末尾追加块，`preflight` 的 step JSON 增加同名键：

```
device snapshot: /path/role-map.json
  source: /path/role-map.json
  schema: ark.device-role-map.v1
  generated_at: 2026-11-11 18:00:00
  mode: role-map
  roles:
    venusa:log: COM13 (resolved)
    ...
```

```json
"device_snapshot": {
  "source": "/path/role-map.json",
  "schema": "ark.device-role-map.v1",
  "generated_at": "2026-11-11 18:00:00",
  "mode": "role-map",
  "roles": {"venusa:log": {"state": "resolved", "port": "COM13", "reason": "..."}}
}
```

不打印整份快照。**退出码沿用既有约定**：快照读不了、schema 不支持、任一 role 解析失败
都按 profile/资产类错误返回 **2**，stderr 只打一行可执行的错误；不会回落到 profile 端口。

```bash
python scripts/burn.py plan --profile scripts/profiles/tcl-pvm1005.json \
  --run-dir /home/zhsh/workspace/tasks/<task>/run --device-snapshot role-map.json
```

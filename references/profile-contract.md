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

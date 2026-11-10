# 结果契约

## 1. 时间契约（硬）

- 时区：北京时间（Asia/Shanghai）。
- 字符串格式：`YYYY-MM-DDThh:mm:ss.sss`，毫秒 3 位。
- **不带任何时区后缀**：`+08:00`、`Z`、`UTC` 都是违约。
- 实现：`core/contract.py: now_bj()`；`is_bj_timestamp()` 会拒绝带偏移的字符串。
- 所有 `at` / `started_at` / `finished_at` / `sent_at` / `updated_at` 都必须满足。

示例：`"2026-11-10T14:02:24.087"`

## 2. Schema

| 产物 | schema |
|---|---|
| profile | `venusahisiburner-profile/v1` |
| 资产 manifest | `venusahisiburner-assets/v1` |
| 状态文件 | `venusahisiburner-state/v1` |
| 步骤回执 | `venusahisiburner-phase/v1` |
| 运行回执 | `venusahisiburner-receipt/v1` |

## 3. 步骤回执（`steps/<step>.json`）

公共字段：`schema`、`step`、`at`、`started_at`、`verdict`（PASS/FAIL/BLOCKED/SKIPPED）、`reason`。

| 步骤 | 关键字段 |
|---|---|
| `preflight` | `ports`、`expected_ports`、`missing_ports`、`versions_before`、`assets.all_ok`、`assets.assets[*].sha_ok/size_ok` |
| `boot:*` | `control.{power_cut,strap_on,power_restore,strap_off}.receipt_ok/reply` |
| `burn:venusa` | `image_sha256`、`image_addr`、`tool_result.rc`、`markers`、`tool_log` |
| `burn:hisi` | `image_sha256`、`cmd`、`optlog`、`optlog_tail`、`tool_exit`、`power` |
| `verify:*` | `versions`、`expectation.{expected,got,match}`、`checks`、`power_cycle`、`serial_logs` 路径 |
| `recover:hisi` | `port`、`command`、`reply_text`、`reply_hex`、`ack` |

## 4. 运行回执（`run.receipt.json`）

```json
{
  "schema": "venusahisiburner-receipt/v1",
  "issue": "NOAH-44",
  "node": "pvm1005",
  "profile": {"name": "tcl-pvm1005", "sha256": "...", "status": "measured", "family": "tcl"},
  "time_contract": {"zone": "Asia/Shanghai", "offset_in_string": "none", "format": "YYYY-MM-DDThh:mm:ss.sss"},
  "started_at": "2026-11-10T14:02:24.087",
  "finished_at": "2026-11-10T14:10:59.759",
  "plan": ["preflight", "boot:venusa", "..."],
  "completed_steps": ["..."],
  "step_receipts": {"step": {"verdict": "PASS", "path": "..."}},
  "assets": {"assets": {"id": {"sha_ok": true, "size_ok": true, "path": "..."}}, "all_ok": true},
  "versions": {"venusa": {"csk_app": "..."}, "hisi": {"hisi_sys_ver": "..."}},
  "control_commands": [{"at": "...", "command": "uut-switch1.on", "receipt_ok": true}],
  "writes": {"power_or_strap_commands": 12, "flashed_chips": ["venusa", "hisi"]},
  "artifacts_sha256": {"steps/preflight.json": "...", "...": "..."},
  "verdict": "PASS",
  "reason": ""
}
```

判定规则：

- `verdict: PASS` 仅当 plan 里除 `receipt` 外全部步骤 PASS、资产全部 `sha_ok && size_ok`。
- 任何一步 FAIL/BLOCKED → `receipt.verdict = FAIL`，`reason` 指明缺失步骤。
- `artifacts_sha256` 覆盖 run 目录内除 `run.receipt.json` 和 `state.json` 外的所有文件
  （`state.json` 在写回执后仍会更新，单独列在 `completed_steps` 里，不参与哈希）。

## 5. 归档门禁

只有 `run.receipt.json` 的 `verdict = PASS` 且 `writes.flashed_chips` 与本次目标一致时，才允许：

1. 生成归档包（zip）并写 `archive.receipt.json`（含 sha256、文件清单、来源任务目录）。
2. 生成推送预览（分支、文件列表、diff、测试结果）并等待审批。
3. 共享归档上传后校验 sha256；上传失败标 BLOCKED，不伪造成功。

FAIL / BLOCKED / NEEDS_REVIEW 一律不归档、不推送。

## 6. 隐私与凭据

- 回执、日志、reset_log 不记录 token、密码、私钥、邮箱口令。
- 物料来源只记邮件 uid / 主题 / 压缩包 sha256，不记邮箱地址与凭据。
- 本机绝对路径可以出现在回执里（`serial_logs`、`optlog`），但对外交付时改用相对路径或归档包内路径。

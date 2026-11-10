# 统一状态机

## 1. 步骤从 profile 推导

```
preflight
for chip in profile.chips (profile 顺序):
    boot:<chip>            仅 boot.required = true
    burn:<chip>
    recover:<chip>         recover.required 且 recover.before_verify = true
    verify:<chip>
    recover:<chip>         recover.required 且 before_verify = false
receipt
```

TCL 台架实际展开：

```
preflight → boot:venusa → burn:venusa → verify:venusa
          → burn:hisi → recover:hisi (AT+FTM=0) → verify:hisi → receipt
```

Media draft 展开：`preflight → burn:csk6 → verify:csk6 → receipt`。

## 2. 依赖与重跑

- 严格串行：`step` 只能在前一个 plan 步骤 `completed` 之后运行。
- 只有 PASS 会写入 `completed`；FAIL/BLOCKED 不会，因此重跑一定会重做失败步骤。
- 状态存 `state.json`（含 `completed`、`receipts`、`failed`），跨进程续跑。
- 单步重跑示例：`python scripts\burn.py step verify:hisi --profile ... --run-dir ... --confirm-write`。

## 3. 守卫

| 守卫 | 行为 | 退出码 |
|---|---|---|
| 缺 profile / schema / 字段 | 拒绝 | 2 |
| profile 继承键 | 拒绝 | 2 |
| draft profile 的写步骤 | 拒绝，列出 `measurement_required` | 3 |
| 写步骤缺 `--confirm-write` | 拒绝 | 3 |
| 台架锁被活进程持有 | 拒绝，不 kill | 3 |
| 未实现驱动（`csk6-batch`） | 拒绝 | 2/3 |
| 资产缺失或 SHA-256 不符 | preflight BLOCKED | 4 |
| 步骤判定 FAIL | 停止后续，`safe_recover` | 4 |

## 4. 失败停止与恢复

- `all` 遇到非 PASS：立即停止，不再执行后续步骤。
- `safe_recover`：串口控制型 profile 会再发一次 `power_restore`，把台架恢复到上电状态，
  记录到 `reset_log.jsonl` 和 `recover/safe-recover.json`；**不修改判定**。
- 失败也会生成 `run.receipt.json`（`verdict: FAIL`，`reason` 里写明哪一步没 PASS），
  保证失败的证据链同样完整。
- 不杀其他进程、不抢串口；锁文件只清理自己持有的。

## 5. 证据布局

```
<run-dir>/
├── state.json                  # completed / receipts / failed
├── reset_log.jsonl             # 每条断电/strap 命令 + 原始回执
├── steps/
│   ├── preflight.json
│   ├── boot-venusa.json
│   ├── burn-venusa.json
│   ├── verify-venusa.json
│   ├── burn-hisi.json
│   ├── recover-hisi.json
│   └── verify-hisi.json
├── tool_logs/
│   ├── tool_venusa.log         # Uart_Burn_Tool 输出
│   └── tool_hisi.log           # BurnTool stdout（通常为空，成功证据在 optLog）
├── serial_logs/
│   ├── preflight_<chip>.log
│   └── verify-<chip>_<chip>.log
├── recover/safe-recover.json   # 仅失败时
└── run.receipt.json
```

BurnTool 的 `optLog_*.txt` 留在 `BurnTool_Gold/optLog/`，`burn-hisi.json` 记录其绝对路径与尾部摘要。

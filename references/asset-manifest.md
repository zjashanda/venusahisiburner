# 统一资产 manifest（OSS + 本地解析）

`assets/manifest.json` 是整个 Skill 的唯一资产登记处：CSK/HISI 工具和固件都在这里引用，
**不复制到任何项目 profile**。

## 1. OSS 块

```json
{
  "schema": "venusahisiburner-assets/v1",
  "oss": {
    "provider": "ark-oss",
    "endpoint_env": "ARK_OSS_ENDPOINT",
    "bucket_env": "ARK_OSS_BUCKET",
    "credential": {
      "mode": "env-only",
      "access_key_env": "ARK_OSS_ACCESS_KEY_ID",
      "secret_key_env": "ARK_OSS_ACCESS_KEY_SECRET",
      "persist": false
    }
  },
  "assets": []
}
```

- 只允许 `mode: env-only` 且 `persist: false`：凭据在下载时从环境读取，不落盘、不写日志、不回显。
- `endpoint_env` / `bucket_env` / `*_key_env` 的值必须是**环境变量名**（大写 `[A-Z0-9_]`）。
- 加载时扫描所有字符串值：出现 `ghp_…`、`AKIA…`、`-----BEGIN … PRIVATE KEY-----`、
  `x-access-token:`、`password=` 直接拒绝；键名像凭据但值不是环境变量名也拒绝。 <!-- secret-scan: allow -->

## 2. 一条资产

```json
{
  "id": "tcl-csk7-v0.0.41",
  "kind": "firmware",
  "target_chip": "CSK7/VenusA",
  "version": "V0.0.41",
  "file": "fw.img",
  "bytes": 16777216,
  "sha256": "e2515c52a17140d04b2f3220f00506c2d860515d0217bbbb0c4c41430c84a028",
  "oss": {
    "key": "firmware/tcl-csk7/V0.0.41/fw.img",
    "object": "fw.img",
    "download": "ossutil"
  },
  "install": {
    "material_rel": "csk/extracted/firmware/fw.img"
  },
  "provenance": {
    "source": "HAS_TCL_CSK7_HI3863_LZX_20261103_TEST_V0.0.41.zip (mail uid 159)",
    "zip_sha256": "1e703ad84be24e94626918d994c07f9f4c3eb2aa066417cb33e6e821a322dac1"
  }
}
```

| 字段 | 规则 |
|---|---|
| `kind` | `tool` 或 `firmware` |
| `target_chip` | 目标芯片，防止把 CSK 工具/镜像给 HISI |
| `version` | 发布版本；工具无版本资源时写来源标识（如 `unversioned-20261027`），SHA-256 才是身份 |
| `bytes` / `sha256` | 必填；本地副本大小或哈希不符即视为不可用 |
| `oss.key` | 不含前导 `/`，与 `object` 一起定位对象 |
| `oss.download` | `ossutil` / `storage-http` / `local-only` |
| `install.material_rel` | 本次材料树中的相对路径（固件必填） |
| `install.cache_rel` | 节点工具缓存的相对路径（工具建议填，避免每项目复制） |
| `provenance` | 来源邮件/压缩包/节点与已知备注，便于追物料 |

## 3. 本地解析优先级

1. `--asset-cache` + `install.cache_rel`（节点级工具缓存，多项目共享）
2. `--material-dir` + `install.material_rel`（本次任务的材料树）

解析到文件后**一律重算 SHA-256 与大小**：不符则 preflight BLOCKED，绝不“先烧了再说”。

## 4. 从 OSS 拉取

```powershell
$env:ARK_OSS_BUCKET = "<bucket>"      # bucket 名不进仓库
$env:ARK_OSS_ENDPOINT = "<endpoint>"  # storage-http 方式用
python scripts\burn.py preflight --profile ... --run-dir ... --asset-cache D:\zhsh\workspace\asset-cache --fetch-assets
```

- `ossutil`：调用 `ossutil cp -f oss://<bucket>/<key> <dest>`，下载后校验 SHA-256，不符即删除并报错。
- `storage-http`：`<ARK_OSS_ENDPOINT>/<key>`，同样校验。
- `local-only`：不联网，缺文件即 BLOCKED。
- 缺 bucket 环境变量或 `ossutil` 不在 PATH：抛 `FetchBlocked`，不猜默认 bucket、不静默回退。

## 5. 已登记资产（2026-11-10）

| id | 类型 | 芯片 | 版本 | SHA-256（前 16） |
|---|---|---|---|---|
| `csk-uart-burn-tool` | tool | CSK7/VenusA | 1.0.10 | `56aefe470dd40afe` |
| `hisi-burntool-gold` | tool | HI3863/WS63 | unversioned-20261027 | `4bd51bc3ee3e32c8` |
| `tcl-csk7-v0.0.41` | firmware | CSK7/VenusA | V0.0.41 | `e2515c52a17140d0` |
| `hisi-ws63-v8-h363t20-wfmv206.111.1.16` | firmware | HI3863/WS63 | V8-H363T20-WFMV206.111.1.16 | `ed2eaf013cc761ed` |

`BurnTool.exe` 需要同目录 DLL（`libburn.dll`、Qt5*.dll 等），节点缓存要保留整个 `BurnTool_Gold` 目录。

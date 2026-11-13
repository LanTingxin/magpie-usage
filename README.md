# Magpie Usage Reporter (`magpie-usage`)

将 **Google Antigravity**（App / CLI / IDE）的原生模型与子代理调用统计，通过官方生命周期钩子自动增量上报至本地 **Magpie**（`POST http://127.0.0.1:3425/v1/magpie/usage/requests`）。

无需修改客户端 `app.asar` 或走代理中转，完整保留原生 Google 登录态、官方内置插件（Workspace / Drive / Gmail / Calendar）与子代理能力，同时在 Magpie 面板获得完整的 Token、费用、缓存命中率、首字耗时与解码输出速度（TPS）统计。

---

## 核心特性

- **零侵入、客户端升级不失效**：纯官方插件规范（`plugin.json` + `hooks.json` + `skills/`），在 `PostInvocation` 与 `Stop` 钩子自动增量同步，纯 Python 3 标准库实现，零第三方依赖。
- **全维度指标采集**：
  - **Token 明细**：输入（`in`）、总输出（`out`）、缓存读取（`cache_read`）、缓存写入（`cache_write`）、思考推理 Token（`reasoning`）。
  - **思考强度（Effort）**：自动识别并归一化 `low` / `medium` / `high` / `xhigh` / `max`。
  - **首字耗时与真实输出速度（TPS）**：同时提取首包耗时（`ttft_ms`）、思考结束后的首可见正文耗时（`first_text_ms`）与流式耗时（`flow_ms` / `ms`），让 Magpie 准确计算剔除思考阶段后的正文解码速度（`token/秒`）。
  - **主会话与子代理聚合**：自动解析 `~/.gemini/antigravity/conversations/*.db` 中主会话与后台 Subagent 的 `root_cascade_id`，归并到同一会话链路。
- **严格幂等与防重复记账**：
  - 自动识别并跳过已通过 `[Magpie]` 网关代理转发的模型调用，避免双重记账。
  - 每条调用使用唯一请求 ID（`rid: agy-<request_id>`），Magpie `IngestReported` 原生幂等去重，支持任意次数安全重放。
- **离线容错、不阻塞对话**：若本地 Magpie 未启动，Hook 在 `1.2s` 超时内静默放行，游标保持安全位置；下次 Magpie 启动后自动补齐期间未上报记录。
- **跨平台支持**：开箱支持 **Windows**、**macOS** 与 **Linux**。

---

## 环境要求

- **Antigravity**（桌面端 App 2.0+、`agy` CLI 或 IDE）
- **Python 3.8+**（Windows 支持 `py -3` / `python3`，macOS / Linux 支持 `python3`，仅使用标准库）
- 本地运行中的 **Magpie**（默认监听 `http://127.0.0.1:3425`，若开启鉴权会自动读取 `~/.config/magpie/caller-keys.json`）

---

## 安装方式

### 方式一：通过 `agy` CLI 一键安装（推荐）

```bash
agy plugin install https://github.com/LanTingxin/magpie-usage
```

安装完成后可通过以下命令确认插件已启用：

```bash
agy plugin list
```

### 方式二：手动克隆到全局插件目录

```bash
git clone https://github.com/LanTingxin/magpie-usage.git ~/.gemini/config/plugins/magpie-usage
```

> 若只需在单个项目工作区内生效，可放置于项目根目录下的 `.agents/plugins/magpie-usage`。

---

## 日常使用与历史补录

插件安装并启用后会在每次对话步骤完成时**全自动静默上报**。你也可以直接在对话中向 Antigravity 提问（会自动触发内置的 `/magpie-usage` 技能）：

- 「查看 Antigravity 到 Magpie 的用量同步状态」
- 「把 Antigravity 历史会话用量补录同步到 Magpie」
- 「演练检查待上报给 Magpie 的 Antigravity 调用统计」

或在终端手动执行同步脚本：

### macOS / Linux

```bash
# 查看游标状态与 Magpie 密钥检测结果
python3 -B ~/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py --status

# 演练扫描（只统计待同步条数，不实际发送请求）
python3 -B ~/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py --dry-run

# 立即将所有未同步的历史会话补录到 Magpie（按 rid 幂等去重）
python3 -B ~/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py --backfill

# 忽略本地游标、从头全量重扫所有会话（适用于重建/恢复 usage.jsonl 后）
python3 -B ~/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py --backfill --reset-cursor
```

### Windows (PowerShell)

```powershell
# 查看游标状态与 Magpie 密钥检测结果
py -3 -B "$HOME/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py" --status

# 演练扫描（只统计待同步条数，不实际发送请求）
py -3 -B "$HOME/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py" --dry-run

# 立即将所有未同步的历史会话补录到 Magpie（按 rid 幂等去重）
py -3 -B "$HOME/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py" --backfill

# 忽略本地游标、从头全量重扫所有会话（适用于重建/恢复 usage.jsonl 后）
py -3 -B "$HOME/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py" --backfill --reset-cursor
```

---

## 环境变量配置（可选）

默认零配置即可工作；如需自定义端口、密钥或目录，可配置以下环境变量：

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `MAGPIE_USAGE_URL` | `http://127.0.0.1:3425/v1/magpie/usage/requests` | Magpie 用量上报接口地址 |
| `MAGPIE_GATEWAY_KEY` | 自动从 `caller-keys.json` 读取 | 显式指定 Magpie Bearer 鉴权密钥 |
| `MAGPIE_CONFIG_DIR` | `~/.config/magpie` | 自定义 Magpie 配置目录（用于查找 `caller-keys.json`） |
| `ANTIGRAVITY_CONVERSATIONS_DIR` | `~/.gemini/antigravity/conversations` | 自定义 Antigravity 本地会话 SQLite 目录 |
| `PLUGIN_DATA` | `~/.gemini/antigravity/plugin_data/magpie-usage` | 同步游标 `state.json` 存储目录（Hook 运行时由 Antigravity 自动注入） |

---

## 开发与校验

```bash
# 运行内置单元测试
python3 -B ./scripts/sync_usage.py --self-test

# 使用官方 CLI 严格校验插件规范
agy plugin validate --strict .
```

## License

[MIT](./LICENSE)

# WorkBuddy Manager Gateway

让主 Agent 派工、检查成果和提出返工，让 WorkBuddy 在**真实可见的桌面聊天**里用自己的模型和工具执行。面向 Windows 的本地任务网关，版本 `0.3.0`。网关自有代码使用 Python 3.11+ 标准库；本地渲染器辅助脚本是项目自写的 Node ESM 文件（Node 22+ 内置 WebSocket）。

```text
用户确定目标与授权
        ↓
主 Agent：派工、检查、返工、验收
        ↓ MCP / 本地命令
Gateway：持久任务记录、执行队列、用量分账
        ↓ 操作系统核验的 127.0.0.1 端口 + 唯一原生 renderer
WorkBuddy 桌面应用：新增可见聊天，用自己的模型与工具执行
        ↓
桌面聊天里可见的过程与结果 → 主 Agent 独立检查实际文件
```

## 当前边界

- **默认后端是 `desktop_visible`**：在 WorkBuddy 桌面端新建一个真实聊天并派发任务，聊天出现在桌面历史里，用户可以像平常一样看见过程和结果。
- **独立 CLI 会话只作为明确标注的兼容模式**（`--backend cli_acp`）。网关**不会自动回退**：桌面通路不可用时任务进入 `blocked`/`uncertain` 并说明原因，不会偷偷改用隐藏会话。
- 网关只连接**操作系统上报为 WorkBuddy 可执行文件所有、且仅绑定 `127.0.0.1`** 的调试端口，并且只在**恰好一个**通过身份探测（`__wbInvoke` + 应用版本 + `win32`）的原生 renderer 上操作。不使用 Chrome、不使用全局键盘鼠标、不接管前台窗口。
- 端口归属核验**只实现于 Windows**（TCP 表 + 进程映像路径）。其他平台可以运行模拟测试，但不宣称支持。
- 后台常驻 worker 空闲时只做本地记录检查（每秒一次），**不调用模型**；只有领取到任务时才碰桌面或 CLI。
- 权限仍走 WorkBuddy 自身流程。网关**不自动同意**任何申请；只允许一次性选项，永久授权不予暴露；无法识别的一次性选项保持未决，需要用户在桌面端自行决定。
- 工作目录用于任务路由，不是操作系统级文件隔离。请先在独立测试目录试用。
- 本项目不承诺免费。模型调用仍受 WorkBuddy 的登录、额度、计费和服务规则约束；未知消耗不会被写成零。
- 桌面请求的 `usage` 有两组计数：顶层 `inputTokens`/`totalTokens`/`contextWindow`，以及 `usage.details` 里的 `promptTokens`/`completionTokens`/`promptCacheHitTokens`/`promptCacheMissTokens`/`promptCacheWriteTokens`。范围是该请求**最终的 `usage.details` 快照**；它是否累计全部内部模型轮次**尚未验证**，所以顶层的 `totalTokens` 也不是全任务账单。
- “请求终态”不等于“副作用已停止”：只有桌面同时报告 `hasActiveChildAgents` 与 `hasRunningBackgroundTasks` 为 `false` 时，网关才把任务标为 `stop_confirmed`。两个标记仍在活动或缺失（未知）时，任务进入 `uncertain`，不宣称已停止。
- 原生桌面权限申请的真实载荷形状尚未实测（见 [验收记录](docs/ACCEPTANCE.md)），因此网关对它的处理保守：只显示、不自动决定。
- 兼容模式的 CLI 转发仍有约 **50 秒**、请求和响应各约 **640 KiB** 的边界；这条限制不适用于桌面通路，但桌面通路的长任务上限也未被证明。

## 环境与运行

需要 Python 3.11+、Node.js 22+（本地渲染器辅助脚本使用内置 WebSocket），以及已安装并登录的 WorkBuddy 桌面端。内置 CLI 与原生接口都随 WorkBuddy 版本变化，升级后需要重新检查。非标准安装位置请用下表变量显式指定；源码不内置盘符或个人目录。

```powershell
python -m buddygate doctor
python -m buddygate status
```

| 变量 | 用途 |
| --- | --- |
| `BUDDYGATE_STATE_DIR` | 私有状态目录；默认在 `%LOCALAPPDATA%\WorkBuddyManagerGateway\state`，与源码分离 |
| `BUDDYGATE_NODE` | Node.js 可执行文件路径；用于兼容模式 CLI 与本地渲染器辅助脚本 |
| `WORKBUDDY_CLI_PATH` | WorkBuddy 内置 CLI 脚本的绝对路径（仅兼容模式需要） |
| `WORKBUDDY_HOME` | WorkBuddy 安装目录，供 CLI 路径发现使用 |
| `BUDDYGATE_CLI_EFFORT` | 只影响兼容模式 CLI 的 reasoning effort，不作用于桌面通路 |

连接配置**不放在环境变量里**，而是保存在私有状态目录的 `desktop-connection.json`。配置文件、任务记录、执行回报和成果可能含私人信息，不能提交到 GitHub。

## 连接桌面入口

桌面入口（本机调试端口）需要 WorkBuddy 启动时开启。网关**不修改应用、不修改原生历史、不强杀进程**。先预览，确认无误再保存：

```powershell
python -m buddygate desktop-connect --executable 'D:\Apps\WorkBuddy\WorkBuddy.exe' --port 61616
python -m buddygate desktop-connect --executable 'D:\Apps\WorkBuddy\WorkBuddy.exe' --port 61616 --save
python -m buddygate desktop-status
```

`desktop-connect` 会依次核对：端口监听者是否唯一绑定 `127.0.0.1`、所有者进程映像是否就是指定可执行文件、候选页面是否为本地 `/devtools/page/*`、以及能否被识别为原生 WorkBuddy renderer。任一项不符即拒绝，不会"换一个端口试试"。

`desktop-status` 是只读检查，不调用模型。若 WorkBuddy 已在运行但入口未开启，只能**正常重启**应用（用它自己的退出菜单）：

```powershell
python -m buddygate desktop-launch                 # 预览：将使用什么可执行文件、端口和参数
python -m buddygate desktop-launch --execute-launch # 仅当应用未运行时后台启动，绝不结束已在运行的实例
```

应用未运行时才会启动，且只启动已安装的可执行文件；已经在运行的实例不会被关闭、重启或改写。启动后入口是否可用，再用 `desktop-status` 核实。

## 派工与返工

准备一个已存在的独立目录，放入任务需要的输入文件，再提交任务。`submit` 和 `rework` 默认自动启动隐藏的常驻 worker：

```powershell
python -m buddygate submit --workspace 'D:\GatewayDemo' --objective '读取 input.csv，按月份汇总金额，写入 summary.json。只改 summary.json；报告来源行数、计算方法和未解决问题。' --key 'demo-monthly-001'
```

- 默认后端为 `desktop_visible`，会在桌面新增一个可见聊天；标题由首条提示词的第一行（明确派工标题）决定。
- 会话只创建**一次**：`create` 的回复不可用时，只按已保存的会话 ID 做只读核对，**不会重建、不会换 ID、不会编辑原生历史**；核对不了就把任务标为 `blocked` 并保留该 ID。
- 兼容模式显式指定：`--backend cli_acp`。
- 保存返回的任务编号与原生 `conversation_id`。**结果不确定时绝不重新提交同一目标**；需要继续时用 `rework` 在同一聊天里开启新轮次。
- 相同请求使用稳定的 `--key`；幂等指纹包含 backend，换后端会被视为不同请求并拒绝复用。

```powershell
python -m buddygate get TASK_ID            # 简短状态 + 原生聊天 ID + 最新结果摘要
python -m buddygate get TASK_ID --full     # 原始记录
python -m buddygate events TASK_ID         # 工具级事件（不含私有思考内容）
python -m buddygate rework TASK_ID --feedback '在原结果中补充排除的无效记录数，其余字段保持不变。'
```

`get`/`list`/`status` 默认只给摘要，不重复整段提示词和工具流水。需要细节时读 `events`。

摘要里的 `acceptance` 如实反映记录状态：`accepted_by_manager` 表示**主 Agent 自己记录了验收**（说明与证据在 `accepted` 事件里），网关既不代验收，也不验证业务正确性；`needs_review` 只表示“待复核”。

验收前必须读取实际文件、核对来源或测试：

```powershell
python -m buddygate accept TASK_ID --note '已检查实际输出' --evidence '逐项对照输入重新核算，月份金额与总额一致；无额外修改。'
```

`accept` 只记录检查结论，不替主 Agent 核对业务内容。任务受理成功、协议握手或文件非空，都不能证明任务完成。

## 用量分账

三个计量渠道**分开记录、不合并**，缺失值保持 `null`：

| 渠道 | 来源 | 范围 | 现状 |
| --- | --- | --- | --- |
| `native` | 桌面会话里该请求的 `usage` | 该请求**最终**的 `usage` 快照 | 顶层 `input_tokens`/`total_tokens`/`context_window`；有 `details` 时再给 `prompt_tokens`/`output_tokens`/`prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`/`prompt_cache_write_tokens`；有 `byCategory` 时给 `context_breakdown`（systemPrompt/conversation/tools/mcp/skills）。接口层是"最后一次调用的快照"，不是累计 |
| `transcript` | 该会话本地 transcript 里逐条 `providerData.usage` | 该次原生请求的**逐模型轮次**累计 | 按 `messageId` 去重、按该次 `conversationRequestId` 过滤；缓存命中取 `inputTokensDetails[].cached_tokens`，路由到 `rawUsage` 的字段按原名另列；reasoning 单列不并入输出；只统计已落盘的调用 |
| `codex` | 显式指定的一个 Codex 线程文件的 `token_count` 事件 | 该文件在派工前后的快照差 | 需要 `--codex-thread` 显式给出，不扫描、不读其他聊天 |

```powershell
python -m buddygate submit ... --codex-thread 'C:\path\to\rollout-....jsonl'
python -m buddygate usage TASK_ID               # 该任务的用量与缺失说明
python -m buddygate usage TASK_ID --refresh     # 只读重读 transcript，不重发任何提示
python -m buddygate codex-usage --thread 'C:\path\to\rollout-....jsonl'
```

逐轮计量的边界（由代码强制，不只是文档约定）：

- 只读本网关自己派工的会话：会话 ID 来自任务记录，路径从经核验的 config 目录（`workbuddy.db` + `projects`）与该会话的 `cwd` 定位，最多按 `<会话ID>.jsonl` 在 `projects` 下一层有界查找；符号链接、联接、越界、多个匹配、超大文件或超长行一律拒绝。
- 只解析 `providerData` 里的 ID、模型名和数字计数。消息正文、思考内容、工具参数与结果、账户资料不读取、不输出、不写日志。
- 归属必须靠身份证明：只统计 `providerData.conversationRequestId` 等于该次原生请求的记录。没有可用 ID 且会话里有多个请求时，返回不可用并说明原因，**不按时间或顺序猜**。
- 原生请求 ID 在首次观察到时就写入任务记录。应用重启后桌面请求列表会丢掉 `clientRequestId`，只剩 `conversationRequestId`/`id`；网关不会把用户自己的客户端 UUID 当成原生 ID，也不会在缺 ID 时把该会话所有轮次混算。
- 同一 `messageId` 的重复记录只算一次；数值冲突则该轮不可用，不取平均。派生值（例如用输入减缓存得到的未命中）标注为 `derived`，能读到原始字段时用原始字段并以原名报告。
- 返工会复用同一可见会话但**新开请求**：上一轮的 ID 不会被带到新一轮，避免把旧轮次算进新请求。
- 已验收任务记录不可修改：`usage --refresh` 对已验收任务只读计算并返回，标记 `stored: false`，不写入、不加事件。
- 这不是官方账单，也不是账户级总量。

规则：

- 只传白名单内的计数（JS 层同样只按白名单复制数字）；`cost`/`estimatedCost` 属于非 token 字段，只记录字段名，**不复制数值、不换算币种**。
- 未观测到的字段是 `null`，不填 0；`cached_input_tokens` 保持 `null`，因为只观测到 `promptCacheHitTokens`，按原名称单独报告，不换名冒充。
- `detail_arithmetic_check` 只检查 `promptTokens == 命中 + 未命中 + 写入` 这类内部算术，**不证明计数完整、可计费或已累计**。
- `reasoning_output_tokens` 单独记录，**不重复加进** `output_tokens`。
- Codex 侧：`output_generation_tokens` 只取 `output_tokens` 增量；`uncached_input_tokens = input - cached`，是未缓存输入，**不是生成**。
- Codex 差值窗口从**派工前快照的最后一条 `token_count`** 到**派工后快照的最后一条**，覆盖整个线程在该时间窗口内的活动（包含同一线程的其他工作），不是单个委派任务的因果成本。
- 两份快照必须来自**同一个线程文件**；跨文件、计数回退（重置/轮转）以及活动条数减少一律判为不可用，不调整、不取绝对值。
- 不推算账单，不承诺节约比例。

## 权限、取消与停止确认

出现权限请求时，通过任务状态查看具体请求、`PERMISSION_ID` 与原生命中选项。请求 ID 和选项 ID 必须使用当前实际返回值：

```powershell
python -m buddygate approve TASK_ID --permission-id PERMISSION_ID --option OPTION_ID --note '已核对请求操作与本次授权范围。'
python -m buddygate cancel TASK_ID
```

- 只有 `allow_once`/`reject_once` 可选；`allow_always` 之类永久授权一律不暴露。
- 桌面通路下，如果原生命中选项里没有网关能识别的一次性选项，网关**不会**替你同意，也不会假装处理：事件为 `permission_requires_desktop_decision`，需要到 WorkBuddy 窗口里决定。
- 取消是协作式的：请求取消后仍要等桌面确认终态；超时未确认会进入 `uncertain`，而不是宣称已停止。
- **停止确认**同样需要证据：请求到达 `completed`/`failed` 只说明这一轮请求结束。只有同时拿到 `hasActiveChildAgents=false` 与 `hasRunningBackgroundTasks=false` 才写 `stop_confirmed=true`；任一为 `true` 或缺失时，先继续观察一个宽限期（默认 30 秒），仍不明确就转 `uncertain` 并记录 `stop_unconfirmed`。
- 请求取消不等于已经发生的动作被撤回。

只有人工检查原进程、会话及相关外部动作，确认已停止后，才可处理 `uncertain`：

```powershell
python -m buddygate resolve TASK_ID --note '已检查原执行器并确认停止，保留现有成果待核对。' --evidence '填写实际检查的进程、会话和成果证据。'
```

`resolve` 把 `uncertain` 改为 `blocked`，记录主 Agent 的说明与证据；网关不能验证这些陈述，也不自动恢复执行或回滚外部动作。

**重启恢复的边界**：网关重启时只把未完成的尝试标为 `uncertain`（`dispatch=unknown`、`observation_resumed=false`），**既不重发提示词，也不接管观察**。已发出的请求仍需人工到桌面聊天核对；网关不会把它宣传成“自动恢复”。

## 接入主 Agent

MCP 使用本地 stdio 入口。宿主可能从任意工作目录启动进程，因此使用 `gateway.py` 的实际绝对路径：

```powershell
python 'D:\Tools\workbuddy-manager-gateway\gateway.py' mcp
```

工具：`buddy_doctor`、`buddy_status`、`buddy_submit`、`buddy_get`、`buddy_list`、`buddy_events`、`buddy_rework`、`buddy_approve`、`buddy_cancel`、`buddy_accept`、`buddy_resolve`、`buddy_usage`、`buddy_codex_usage`、`buddy_desktop_status`、`buddy_desktop_connect`、`buddy_desktop_launch`、`buddy_stop`。

`buddy_submit` 的 `backend` 默认 `desktop_visible`；`cli_acp` 只是兼容模式。`buddy_get` 返回摘要，`full=true` 返回原始记录。本项目不会自动改写所有客户端的全局配置。

## 开发检查

```powershell
python -m unittest discover -s tests -v
node scripts/check_desktop_helper.mjs
```

CI 覆盖 Windows、Ubuntu 的 Python 3.11/3.12 标准库测试，并对渲染器辅助脚本做语法检查与模拟行为检查。**模拟测试不证明本机 WorkBuddy 已经连接，也不证明桌面通路已验收**；真实验收另见 [ACCEPTANCE.md](docs/ACCEPTANCE.md)。

同类方案和选择依据见 [COMPARISON.md](docs/COMPARISON.md)，参考与实现说明见 [NOTICE.md](NOTICE.md)。本项目采用 [MIT License](LICENSE)，与 WorkBuddy 官方没有隶属或背书关系。

## 公开发布

公开文件白名单见 [PUBLISH_MANIFEST.txt](docs/PUBLISH_MANIFEST.txt)，每行一个仓库相对路径。打包或上传前检查这些文件的实际内容，并核对清单已涵盖最终源文件与测试（含 `buddygate/desktop_console.mjs`、`buddygate/usage_ledger.py`、`scripts/check_desktop_helper.mjs` 与 `tests/test_usage_ledger.py`）。不要直接压缩整个工作目录；缓存、状态、任务日志、凭据和用户成果不属于发布内容。

```powershell
python scripts/package.py --output '..\workbuddy-manager-gateway-0.3.0.zip'
python scripts/publish_to_github.py --owner VERIFIED_OWNER --repo workbuddy-manager-gateway --check
```

真实发布必须得到公开上传授权，并核实 `--owner` 对应的账号。**由主 Agent 通过本网关派工，让 WorkBuddy 实际运行发布脚本；主 Agent 只准备与核验，不代传后归属给 WorkBuddy。**

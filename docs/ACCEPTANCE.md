# 验收记录

0.3.0 记录日期：2026-10-28。0.2.1、0.2.0 与 0.1.0 记录保留在文末。

自动化、真实执行、内容检查与公开上传分别记录；不保存私人原始任务日志、账号配置或引导计划。**模拟测试通过不代表本机已连接，也不代表桌面通路已验收。** 本文件只记录主 Agent（WorkBuddy）自己跑出的证据；经理的独立核对与 `accept` 由经理完成，本轮未由主 Agent 调用 `buddy_accept`，0.3.0 也**未获经理整体验收**，未 commit、未 push。

## 0.3.0：逐轮已落盘用量与请求身份恢复

| # | 变更 | 实现边界 | 自动化证据 | 真实证据 |
| --- | --- | --- | --- | --- |
| 1 | 逐模型轮次计量缺失：接口层只给最后一次调用的 `usage` 快照 | 新增 `buddygate/usage_ledger.py`：只读同一会话 transcript 的 `providerData.usage` 逐条累计；按 `messageId` 去重（相同只算一次，冲突则整轮不可用、不取平均）；按该次 `conversationRequestId` 过滤；缓存命中取 `inputTokensDetails[].cached_tokens`，`rawUsage.prompt_cache_miss_tokens` 等按原名另列；reasoning 单列不并入输出；非 token 字段（credit/cost/cache_read/cache_write）只记名不换算 | `tests/test_usage_ledger.py` 33 项：多轮求和与通道分离、同 ID 去重、冲突不平均、坏行/截断/超长行、越界与符号链接、多请求不混轮、缓存推导、reasoning 不重复、快照对照、`usage_view` 三渠道分离 | 见下方“probe 数字核对” |
| 2 | 会话路径与内容读取需要硬边界 | 只从经核验的 config 目录（`workbuddy.db` + `projects`）与该会话 `cwd` 定位；`projects` 下一层按 `<会话ID>.jsonl` 有界查找；符号链接/联接、越界、多匹配、超大文件、超长行一律拒绝；只解析 ID、模型名与白名单数字计数，不读消息正文、思考、工具内容 | `PathGuardTests` 5 项（越界、多匹配、超大文件、链接、未核验目录）；解析白名单逻辑见 `_scan` | 真实目录上只输出数字与 ID，未打印任何正文 |
| 3 | 应用重启后 `requests.items` 丢掉 `clientRequestId`，只剩原生 canonical ID | JS `status` 先按客户端 ID 匹配，只有客户端字段缺失时才用**已保存的** canonical ID 匹配；两条以上命中判为歧义并拒绝；`desktop_exec` 在首次观察到就写 `native_request_id`/`native_request_id_source`（绝不覆盖），并把该 ID 传给后续观察；全部条目都缺 `clientRequestId` 且未保存过 ID 时记 `request_identity_lost`，保守判不可用，不按时间猜 | `scripts/check_desktop_helper.mjs` 新增 3 组断言（重启后仅凭 canonical 命中、客户端 UUID 不得当 canonical、歧义拒绝）；`RequestIdentityTests` 5 项 | 真实任务 `task-536071c6-…` 由 0.2.1 worker 执行，未保存 canonical ID，其会话含 2 个请求：只读刷新返回 `available=false`，理由为“2 个请求且无 native request id，拒绝猜”，未按时间匹配 |
| 4 | 返工复用会话时会把上一轮 canonical ID 带进新请求（本轮新发现的真实缺陷） | `store.rework_task` 与新请求预留处都清空 `native_request_id*`、冲突标记与旧 `activity`，避免把旧轮次算进新请求、或用旧 ID 匹配到旧请求 | `test_rework_does_not_carry_the_previous_canonical_id`、`test_rework_reuses_the_conversation_but_reserves_a_new_request` | 缺陷由阅读代码与实际返工任务（attempt 2）状态推出，非模拟 |
| 5 | 已验收记录的不可变性与“只读刷新”冲突 | `usage --refresh` 对 `accepted` 任务只读计算并返回，标记 `stored=false`/`read_only=true`，不写 usage、不加事件；其他状态仍写入并记 `usage_refreshed` | `CliRefreshTests` 2 项（可归属时返回数字且记录不变、不可归属时保持 unavailable） | 真实已验收任务 `task-536071c6-…` 刷新：`status=accepted`、`stored=false`、`prompt_resent=false`，记录未被改动 |
| 6 | 身份自相矛盾时的计量口径 | 两次观察到不同 canonical ID 时，保留首个、写 `native_request_identity_conflict`，逐轮用量直接不可用（也不走“单请求会话”回退），绝不重发 | `test_two_canonical_ids_for_one_dispatch_are_not_averaged`、`test_a_conflicting_canonical_id_blocks_attribution` | 未在真实请求中触发（属防御路径） |
| 7 | 发布清单未覆盖新文件（本轮唯一自动化失败项） | `docs/PUBLISH_MANIFEST.txt` 补入 `buddygate/usage_ledger.py`、`tests/test_usage_ledger.py` | `test_publish_manifest_covers_every_source_file` | `scripts/package.py` 白名单打包 41 个文件，含两新文件，无 `.pyc` |

### probe 数字核对（只读，仅数字）

对用户授权的 probe 会话 `b7c7b467-b0d5-4659-8446-c34f382bc4ec`（本网关自己的会话）用当前代码重算，与 `work/native-usage-audit-result.json` 逐项对照：

| 指标 | 报告值 | 本轮实测 | 结论 |
| --- | --- | --- | --- |
| 轮次 | 5 | 5 | 一致 |
| 输入 token | 166456 | 166456 | 一致 |
| 输出 token | 1479 | 1479 | 一致 |
| 合计 token | — | 167935 | 等于输入 + 输出（5/5 轮满足 `totalTokens = input + output`） |
| 缓存命中 | 132992 | 132992 | 一致（`inputTokensDetails[0].cached_tokens`） |
| 未命中 token | 33464 | 33464 | 数值一致；来源是 transcript 自带的 `rawUsage.prompt_cache_miss_tokens`（reported），因此 `cache_miss_derived=false`。审计报告写“derived”是审计方按“输入 − 命中”推导，两者数值相等 |
| reasoning（单列） | 139/8/0/508/0 | 655 合计 | 逐轮一致，未并入输出 |
| 模型 | deepseek-v4.1-flash | 同一 ID | 一致 |
| 归属方式 | — | `single_request_in_conversation` | 该会话只有一个请求，无 native ID 时按唯一请求归属 |

`rounds_without_usage=10`（有 `providerData` 但无 `usage` 的记录）、`parse_errors=0`、`compacted_records=0`、`invalid_fields=[]`、`completeness=all_recorded_rounds`。

## 本轮质量门（0.3.0）

| 项目 | 状态 | 证据 |
| --- | --- | --- |
| 全量 Python 测试 | 通过 | `python -m unittest discover -s tests` 共 **138 项通过，1 项跳过**（跳过项为本机不允许创建符号链接的链接拒绝用例），约 24.6 秒 |
| Node 语法与协议 | 通过 | `node --check buddygate/desktop_console.mjs`、`node scripts/check_desktop_helper.mjs`（含 details/byCategory 白名单、活动三态、canonical 匹配与歧义拒绝） |
| 白名单打包 | 通过 | `python scripts/package.py` → 41 个文件，`sha256 80cd713ef65b72d7…`，无 `.pyc`/状态/日志 |
| probe 数字 | 通过 | 见上表，5 轮四类数字全部与报告一致 |
| 已验收任务只读刷新 | 通过（保守不可用） | `task-536071c6-c60c-4000-bced-4b45d1ffa70c`：`stored=false`、`available=false`、未重发、记录未改；按经理要求未重跑、未再验收 |
| 经理整体验收 | **未进行** | 本轮未调用 `buddy_accept`，也未 commit/push |

## 仍未验证（0.3.0，不得当成已通过）

- **真实桌面权限申请**的字段形状与真实载荷决议语义（仍只有模拟覆盖）。
- **真实取消**完整链路，以及后台子任务导致的真实 `stop_unconfirmed`。
- **真实长会话**：`max_rounds=512`、`max_bytes=32 MiB`、`max_line_bytes=256 KiB` 的截断分支只有模拟，没有真实超长会话。
- **失败重试完整性**：模型调用失败/重试时是否留下 `usage` 记录、缺口如何表现，未在真实失败中观测。
- **真实重启后继续观察**：仅验证了身份能保存、缺证据时保守不可用；跨进程“恢复观察”没有实现，也不在 0.3.0 承诺内。
- 带真实 Codex 线程文件的计量（`usage.codex` 仍为 null）。
- `desktop-launch --execute-launch` 的真实启动路径、远端 Windows/Ubuntu CI、公开上传。

## 0.2.1 返工：六项有证据的问题

| # | 问题 | 修正 | 自动化证据 | 真实证据 |
| --- | --- | --- | --- | --- |
| 1 | JS 丢弃 `usage.details` 嵌套，`meter` 硬编码输出/缓存为 null | JS 只按白名单复制已证实计数：顶层 `inputTokens`/`totalTokens`/`contextWindow`，`details` 的 `promptTokens`/`completionTokens`/`promptCacheHitTokens`/`promptCacheMissTokens`/`promptCacheWriteTokens`，以及 `byCategory` 的上下文构成；`cost`/`estimatedCost` 只记字段名、不取值 | `scripts/check_desktop_helper.mjs` 断言 details 五字段、`byCategory` 五部分、`cost` 只入 `ignored_fields` 且序列化结果不含币种字符串；`test_details_usage_is_exposed_with_scope_and_without_cost`、`test_context_breakdown_is_reported_with_an_arithmetic_check`、`test_native_details_are_mapped_by_name_and_stay_separate_from_top_level` | 真实请求 `01a181ba-dbae-735f-940c-575b5bc11770`：`input_tokens/total_tokens=37202`、`context_window=300000`、`context_breakdown={systemPrompt 6459, conversation 0, tools 27521, mcp 503, skills 2719}`（合计 37202），`cost` 仅记名。该请求**没有** `details`，五个 details 字段保持 null |
| 2 | `codex_delta` 不校验线程文件、负数被夹到 0、`window.first` 取错、把未缓存输入当生成 | 跨文件/计数回退/活动条数减少一律判不可用；`window.first = before.window.last`；`output_generation_tokens = output` 增量；新增 `uncached_input_tokens = input - cached`；拒绝 bool/NaN/Infinity/负值/小数 | `test_codex_delta_refuses_to_mix_two_thread_files`、`test_codex_reset_or_negative_increment_is_unavailable`、`test_codex_missing_counters_are_reported_not_estimated`、`test_invalid_token_counters_are_refused` | 未指定真实 Codex 线程文件，本轮 `usage.codex=null`（仍为未实测项） |
| 3 | 只看请求终态就写 `stop_confirmed=true`，可能忽略仍在运行的子 Agent/后台任务 | JS 传 `hasActiveChildAgents`/`hasRunningBackgroundTasks`（三态，缺失即 unknown）；`desktop_exec.activity_status` 只有在两者都为 `false` 时才算 `clear`；活动或未知时继续观察宽限期（默认 30s），仍不明则 `uncertain` 并记 `stop_unconfirmed` | `test_terminal_request_with_active_child_agents_is_not_confirmed_stopped`（且返工被拒）、`test_unknown_activity_flags_are_not_treated_as_stopped`、`test_activity_that_clears_allows_stop_confirmation` | 真实请求终态 `completed`，`hasActiveChildAgents=false`、`hasRunningBackgroundTasks=false`，因此 `stop_confirmed=true`，`stop_confirmed_basis` 写明依据；任务结束后只读复核仍为 `idle` + 两标记 false |
| 4 | `create_conversation` 每秒重放原生 `create`；`reused_conversation` 在覆写后才计算 | `create` 只调一次；回复不可用时用已保留 ID 做只读 `get`（新增 `conversation` 操作），确认不了就报未知并保留 ID，绝不重建/换 ID；`reused_conversation` 从覆写前的原始状态计算 | `CreateOnceTests` 4 项（确认路径、只读核对、读取失败、核对为不存在；均断言 `create` 恰好一次）、`test_reused_conversation_is_recorded_from_the_original_state`；helper 脚本断言 `conversation` 不触发 create | 真实任务事件：`conversation_created` 恰好 1 次、`prompt_dispatched` 恰好 1 次；`dispatch_reserved.reused_conversation=false` |
| 5 | `acceptance` 对已验收任务仍恒写 Not accepted；恢复被说成自动接管 | 摘要按状态返回 `accepted_by_manager` / `not_accepted_yet` / `not_accepted`，并注明网关不验证业务正确性；恢复只标 `uncertain`（`dispatch=unknown`、`observation_resumed=false`），文档标明“不重发、不接管观察” | `test_acceptance_text_reflects_what_was_recorded`、`test_restart_recovery_never_resends`（断言 `observation_resumed=false`、`prompt_resent=false`、恢复后零发送且零观察） | 恢复边界为代码与文档声明，**未在真实崩溃中实测** |
| 6 | 不把自测当独立验收 | 本轮未调用 `buddy_accept`；真实权限载荷、真实取消、实际启动、远端 CI、公开上传继续按未验证记录 | — | 见下方“仍未验证” |

### 额外发现（超出六项，已修）

`buddygate/discovery.py` 从初版起内置了 `D:/workbuddy` 这个个人安装盘符作为发现候选，等于把个人路径带进公开源码。该候选在这台机器上与注册表结果重复，属于冗余；已删除，非标准安装位置改为显式提供（`WORKBUDDY_HOME` / `BUDDYGATE_CLI`）。删除后 `doctor` 仍通过注册表解析到同一 CLI（2.165.0），`tests/test_discovery.py` 9 项通过，并新增一项源码扫描测试防止个人路径再次进入。

## 本轮质量门

| 项目 | 状态 | 证据 |
| --- | --- | --- |
| 全量自动化 | 通过 | `python -m unittest discover -s tests` 共 **99 项通过**，约 19.2 秒（上一轮实测 82 项，本轮新增 17 项返工回归） |
| 渲染器辅助脚本 | 通过 | `node --check buddygate/desktop_console.mjs`、`node scripts/check_desktop_helper.mjs`（含 details/byCategory 白名单、cost 不取值、活动三态、`conversation` 只读） |
| 端口归属与单一 renderer | 通过（真实） | `desktop-status`：监听仅 `127.0.0.1`，所有者 PID 56752 → `D:\workbuddy\WorkBuddy.exe`（5.9.3 / win32），候选页面 1 个 |
| 真实桌面派工 | 通过（真实） | 任务 `task-95b09135-5991-4913-bfd4-202390a2e2ca`，会话 `01a181ba-daa6-7371-ac37-67ece0b0f9ab`，请求 `01a181ba-dbae-735f-940c-575b5bc11770`；`completed`，`needs_review`，`stop_confirmed=true` |
| 真实内容检查 | 主 Agent 已核对，待经理独立复核 | `work/desktop-smoke-4/smoke-result.json` 40 字节精确为 `{"checked": true, "source": "input.txt"}`；`input.txt` 74 字节未变 |
| 旧任务与他人改动 | 保留 | 旧任务状态、`session_id`、指纹不变；`tests/test_discovery.py`、`worker.py` 的 `BUDDYGATE_CLI_EFFORT` 改动未被回退 |

## 本轮真实派工中的用量与停止语义

| 字段 | 值 | 含义与限制 |
| --- | --- | --- |
| `input_tokens` / `total_tokens` | 37202 / 37202 | 该请求报告的计数；**同一快照、同一数值，未证明是全任务累计消耗** |
| `context_window` | 300000 | 模型上下文窗口，不是消耗量 |
| `context_breakdown` | systemPrompt 6459 / conversation 0 / tools 27521 / mcp 503 / skills 2719 | 同一请求的上下文构成；合计 37202 与 `inputTokens` 相等（内部算术检查），**不是生成量，也不是账单** |
| `prompt_tokens`、`output_tokens`、缓存三项 | `null` | 本次响应的 `usage` 没有 `details` 快照（另一次由经理查询的会话有），缺失保持 null |
| `cost` | 不记录数值 | 非 token 字段，只记字段名；币种与口径未核实，不推断 |
| `usage.codex` | `null` | 未指定 Codex 线程文件 |
| `combined` | `null` | 两渠道不合并，不推算价格或节约比例 |
| `hasActiveChildAgents` / `hasRunningBackgroundTasks` | false / false | 来自桌面 `info`，是判定 `stop_confirmed` 的必要条件；缺一即 unknown，不得视为已停止 |

补充观察：5.9.3 的真实 `usage` 形状**不固定**——同一台机器上，一次请求带 `byCategory` + `cost`，另一次（经理查询的 `b7c7b467-…` 会话）带 `details`。因此实现按“出现才传、缺失为 null”处理，不假设形状恒定。

## 仍未验证（不得当成已通过）

- **真实桌面权限申请的字段形状**与真实载荷下的决议语义（模拟只覆盖已识别形状）。
- **真实取消**链路，以及后台子任务活动导致 `stop_unconfirmed` 的真实现象（本轮模拟覆盖）。
- `desktop-launch --execute-launch` 的真实启动路径。
- 真实崩溃后重启恢复（本轮只验证恢复标记与零重发/零接管观察）。
- 远端 Windows/Ubuntu CI、公开上传。
- 带真实 Codex 线程文件的计量（`usage.codex` 仍为 null）。
- Codex 侧 5 个仍在运行的旧 MCP 进程：由经理在稳定后重连，本轮不改全局配置、不结束应用进程。

## 0.2.0：桌面可见聊天与用量分账（2026-10-28）

| 项目 | 当前状态 | 通过条件 | 实际证据 |
| --- | --- | --- | --- |
| 标准库自动化测试 | 通过（已被 0.2.1 的 98 项取代） | 全量 unittest 通过，并列出真实数量 | 当时记录 80 项通过，约 17.3 秒 |
| 渲染器辅助脚本检查 | 通过 | 语法检查与模拟行为检查均通过，可在 Linux 运行 | `node --check` 与假 `__wbInvoke` 的模拟检查通过，不接触真实账号 |
| Windows / Ubuntu CI | 未运行 | 公开 CI 对应提交的全部矩阵作业通过 | 本地脚本已按 CI 步骤执行；远端结果待上传后填写 |
| 端口归属核验（真实） | 通过 | 仅连接操作系统报告为 WorkBuddy 所有、且只绑定 127.0.0.1 的端口 | 监听仅 `127.0.0.1`，所有者 PID 56752 映像为 `D:\workbuddy\WorkBuddy.exe` |
| 桌面真实派工（首轮） | 通过 | 新增真实可见聊天，用 WorkBuddy 自己的模型执行，过程可见 | 任务 `task-59a21ed4-f58e-42e4-a982-739ec7d0a23c`，会话 `01a181a0-c849-7485-9dcf-e4c81b1f7344`；`prompt_dispatched` 恰好一次 |
| 先持久化 ID 再触发副作用 | 自动化通过 | 发送前已写入会话与请求 ID | 在 `send_prompt` 内回读记录：`dispatch=reserved`、`status=running` |
| 重复提交与重放 | 自动化通过 | 同一尝试不二次发送；重启恢复不重放 | `dispatch_refused` 且零发送；恢复改 `unknown` 后仍零发送 |
| 返工复用同一聊天 | 自动化通过 | 保留会话 ID，但分配新 request ID | 第二轮不新建会话，`conversation_id` 相同、`request_id` 不同 |
| 旧任务迁移 | 自动化通过 | 旧任务保持 CLI 语义，不回写指纹 | 旧行回填 `cli_acp`，`session_id`/`result` 不变，重复打开幂等 |
| 不打扰用户桌面 | 通过 | 不用外部浏览器、全局键鼠；不关闭或重启应用；不改原生历史 | 全部操作经本机核验端口与单一 renderer |

## 0.1.0 基线（2026-10-27）

| 项目 | 状态 | 证据摘要 |
| --- | --- | --- |
| Windows / Python 版本与入口 | 通过 | Windows / Python 3.12.14 / WorkBuddy 5.8.3 / CLI 2.161.0 |
| 标准库自动化测试 | 通过 | 当时的 50 项全量通过，约 7.8 秒 |
| 原生 IPC 与账号模型目录 | 局部通过（只读） | 随包 IPC 库返回模型目录 HTTP 200、code 0；不读取登录令牌 |
| 本机转发端点白名单 | 通过 | 仅无 query 的 `POST /v2/chat/completions`；其他路由 403 |
| CLI ACP 握手与模型目录 | 通过 | initialize / session/new / session/load 成功 |
| 原生工具实际执行 | 通过 | WorkBuddy 自己的 Write/Read 创建并读取测试文件 |
| 真实内容检查 | 通过 | 独立读取磁盘字节并逐行核对 |
| 同任务、同会话返工 | 通过 | 新进程恢复同一会话，仅修改要求部分 |
| 幂等提交 | 自动化通过 | 同键复用、冲突拒绝、跨进程抢领 |
| Worker 重启恢复 | 局部通过 | 模拟覆盖崩溃转 uncertain；未实测执行中真实崩溃 |
| 权限允许与拒绝 | 局部通过 | 真实 CLI 逐项 allow_once；模拟覆盖永久选项、过期请求、并发审批 |
| 取消与中断 | 自动化通过 | 模拟子进程验证 cancel 与终态；未中断真实业务 |
| MCP 握手与工具调用 | 通过 | stdio 客户端完成初始化、列工具与实际查询 |
| 桌面连接器继承 | 未验证 | 逐项连接器需单独实测 |
| 公开上传 / 远端 CI | 未实测 | 由 WorkBuddy 实际完成并核对 |

## 最小真实任务（桌面通路）

1. 建立全新测试目录，准备简单输入，限制允许修改的文件。
2. `desktop-connect` 预览 → 核验 → `--save`；不要跳过核验直接写配置。
3. 主 Agent 提交带稳定幂等键的任务，保存 task / conversation / request 身份。
4. Worker 通过桌面会话执行；只观察对应 request，不等待长调用，不重复发送。
5. 主 Agent 独立读文件、核对内容；`accept` 由经理自己决定。
6. 执行中断或不确定时只检查与明确恢复决策，禁止为验收重复投递同一目标。

## 交付结论

桌面可见派工、先持久化后发送、不自动回退、不自动同意权限、请求终态不等于副作用停止、用量按来源与范围分账且缺失保持 null，已由自动化与真实派工验证；真实权限载荷、真实取消、实际启动路径、真实崩溃恢复、真实 Codex 计量、远端 CI 与公开上传仍按上表保留未验证。

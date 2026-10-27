# 同类方案与实现选择

审查日期：2026-10-27。以下是公开源码与文档的审查结论，不表示已在当前电脑运行全部候选。

| 方案 | 真正执行方式 | 可取之处 | 本项目需要处理的差异 |
| --- | --- | --- | --- |
| [dudu555-djiao/codex-workbuddy-gateway](https://github.com/dudu555-djiao/codex-workbuddy-gateway/tree/fbaaa2ef6d6cb59f48b953a1f27058202a81553f) | macOS 桌面内部 RPC 唤醒，原生会话领取本机队列 | 持久队列、幂等、原会话返工、单独验收；公开记录了失败与未验证边界 | macOS LaunchAgent、应用路径和进程检查；不是可直接在 Windows 部署的版本。审查时未见明确 LICENSE；main 比 v0.7.0 标签超前一提交，版本与清单需要区分 |
| [jie023/workbuddy-acp-bridge](https://github.com/jie023/workbuddy-acp-bridge/tree/e2327499273efa093ebdcbffde9a4c8833e26e76) | Windows 桌面 Remote Gateway 的 HTTP/SSE ACP，创建隐藏会话 | 后台访问已有连接器；默认拒绝权限、避免自动重放不确定提交 | 依赖内部 Gateway；全局单会话，每轮先 `/clear`，不适合直接保留多个项目的返工上下文；审查时未见明确 LICENSE |
| [q1lon/workbuddy-acp-bridge](https://github.com/q1lon/workbuddy-acp-bridge/tree/74ab24fb9eb96ae83719feabf8bcf35e79ae2655) | 同类 HTTP/SSE ACP | 有模拟测试与跨平台说明 | 核心仍是全局单会话和每轮清空；未见持久多任务调度与独立验收；审查时未见明确 LICENSE |
| [BenjaminNH/workbuddy-subagent-bridge](https://github.com/BenjaminNH/workbuddy-subagent-bridge/tree/6ffbb7484c93dee564385a96929633715ea3022d) | WorkBuddy 内置 CLI 的 stdio ACP | MIT；作者记录 Windows 实测；独立任务、原会话反馈、逐项权限回包；无需桌面调试端口 | MVP 默认模型写死；报告只存内存；请求超时、异常恢复、取消确认与日志上限需要补齐。独立 CLI 不能直接等同于桌面连接器会话 |
| [jiang198012/workbuddian](https://github.com/jiang198012/workbuddian/tree/568e7596bab6efe9c6173d0b2c62102bf156afa2) | 宿主 WBIPC 账号请求代理 → 临时本机 HTTP → 内置 CLI 工具执行 | MIT；账号凭据留在宿主；有目录模型配置、超时、大小及不自动重放边界 | 本项目参考其 Native 账号通路，以安装包自带 IPC 库连接 Windows 宿主，自行实现 Python 转发与任务网关；保留约 50 秒、640 KiB、完整缓冲限制。上游 Windows 模拟不替代本机验收 |
| [LinHaiJ/workbuddy-mcp](https://github.com/LinHaiJ/workbuddy-mcp/tree/9c8f3ef6d1b5cdb9575dbfef7f9ff51b672b3682) | `codebuddy -p` 的一次性命令行执行 | MIT；接口小、配置简单 | 默认 `--dangerously-skip-permissions`；没有完整多轮持久任务管理。审查版本使用 `shell:true` 并把未校验的模型参数传入 shell，不宜原样采用 |

## 为什么选择独立实现

本项目选用“主 Agent → 持久任务网关 → 内置 CLI stdio ACP”，由 CLI 实际执行工具；CLI 的模型请求经带 nonce 的临时 `127.0.0.1` 转发，再通过已登录桌面宿主的 `wb.request/http.fetch` 完成账号鉴权。独立 CLI 在当前 WorkBuddy 5.8.3 环境不会自动继承桌面登录，单纯握手不能证明可以执行任务。

网关自有代码使用 Python 标准库，运行时加载 WorkBuddy 官方随包 IPC 库，不分发该库或 CLI。工作进程使用独立 profile，不改已有账号设置；默认要求 WorkBuddy 已运行并登录，不启动或操作桌面窗口，也不开放桌面调试端口。参考了公开接口行为与架构思路，未复制无明确 LICENSE 的仓库代码。

网关保存任务与轮次，Worker 负责实际执行，主 Agent 检查真实产物并决定返工或验收。需要恢复时查原任务；不能因重启、超时或短时无进度自动重复执行业务目标。

当前本机转发仅允许无 query 的 `POST /v2/chat/completions`，依据是小文件任务中实际观测到的路径；其他模型协议明确不支持，不静默回退。初始化模型目录的只读查询单独处理。网关不重派不确定的业务目标，但底层 CLI 的连接或网络重试仍可能发生，不保证所有上游请求绝对不重复。

这条路线仍有验证成本：WorkBuddy 的 CLI 参数、随包 IPC 库、ACP 事件、登录、模型与原生权限可能变化；桌面连接器是否可用要逐项确认。原生 HTTP 转发完整缓冲，单次请求约 50 秒，请求和响应各约 640 KiB；不能从目录查询或小任务成功推断长期复杂任务可用。实际收益需要结合任务耗时、主 Agent 检查成本、WorkBuddy 消耗和故障恢复次数判断，不以 Agent 数量推断省钱。

本项目不提供 OpenAI/Anthropic 兼容模型接口，不把执行器当作额度转发服务，不保证免费调用或无人值守完成所有任务。

# 实现与参考说明

WorkBuddy Manager Gateway 的网关代码为独立的 Python 标准库实现。设计参考了公开资料中的任务委派、ACP 会话、权限事件、原生账号请求通路、执行回报与独立验收流程；没有复制下列未提供明确 LICENSE 的仓库代码，也没有直接移植 TypeScript 实现。

运行时通过已安装 WorkBuddy 自带的 `workbuddy_ipc` 库连接宿主的 `wb.request/http.fetch`，该厂商库及 WorkBuddy CLI 不包含在本项目分发内容中。临时本机转发使用本次进程生成的 nonce；账号登录鉴权仍由 WorkBuddy 宿主管理。

| 参考 | 本项目使用的知识 | 审查时的许可信息 |
| --- | --- | --- |
| [BenjaminNH/workbuddy-subagent-bridge](https://github.com/BenjaminNH/workbuddy-subagent-bridge) | Node 启动内置 CLI 的 stdio ACP 路线、会话与权限事件的接口行为 | MIT |
| [jiang198012/workbuddian @ 568e7596bab6efe9c6173d0b2c62102bf156afa2](https://github.com/jiang198012/workbuddian/tree/568e7596bab6efe9c6173d0b2c62102bf156afa2) | `workbuddyNative.ts`、`workbuddyBroker.ts`、`workbuddyModels.ts` 的宿主账号请求通路、临时本机转发、目录模型配置与边界处理 | MIT；确切版权见下文 |
| [dudu555-djiao/codex-workbuddy-gateway](https://github.com/dudu555-djiao/codex-workbuddy-gateway) | 持久任务、明确执行归属、返工与独立内容验收的架构思路 | 2026-10-27 审查时未见明确 LICENSE |
| [jie023/workbuddy-acp-bridge](https://github.com/jie023/workbuddy-acp-bridge) / [q1lon/workbuddy-acp-bridge](https://github.com/q1lon/workbuddy-acp-bridge) | 对照桌面 HTTP/SSE ACP 与独立 CLI ACP 的差异 | 2026-10-27 审查时未见明确 LICENSE |

以上记录说明本项目的参考范围，不构成对其他项目权利状态的法律判断。若后续引入第三方代码，必须先核对许可，并在此保留相应来源与版权声明。

Workbuddian 在该固定提交的 [LICENSE](https://github.com/jiang198012/workbuddian/blob/568e7596bab6efe9c6173d0b2c62102bf156afa2/LICENSE) 列示如下版权，作为引用来源一并保留：

```text
Copyright (c) 2025-2026 jiang198012
Copyright (c) 2025 ben4202121 (BuddyBridge: https://github.com/ben4202121/buddybridge)
```

其上游 [NOTICE](https://github.com/jiang198012/workbuddian/blob/568e7596bab6efe9c6173d0b2c62102bf156afa2/NOTICE) 说明早期版本参考并使用了 BuddyBridge 的部分基础设施，后续独立重构后仍保留原版权。此处保留来源，不表示上述项目为本项目背书。

WorkBuddy、CodeBuddy 及相关产品名称属于其各自权利方。网关不随包分发 WorkBuddy 程序，不提供或转售账号、模型额度及连接器凭据，也不代表官方支持或接口稳定性承诺。WorkBuddy 软件和服务仍适用其自己的使用条件。

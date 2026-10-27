# WorkBuddy Manager Gateway

让主 Agent 派工、检查成果和提出返工，让 WorkBuddy 使用自己的工具实际执行。面向 Windows 的本地任务网关，版本 `0.1.0`。网关自有代码使用 Python 3.11+ 标准库；账号通路加载已安装 WorkBuddy 自带的 IPC 库，不随包分发厂商代码。

```text
用户确定目标与授权
        ↓
主 Agent：派工、检查、返工、验收
        ↓ MCP / 本地命令
Gateway：持久任务记录与执行队列
        ↓ ACP stdio
WorkBuddy 内置 CLI：独立后台会话与原生工具
        │ 模型请求
        ↓ 临时 nonce 认证的 127.0.0.1 转发
已登录 WorkBuddy 宿主：WBIPC wb.request/http.fetch
        │ 宿主管理账号鉴权
        ↓ WorkBuddy 服务
实际文件 / 执行回报 → 主 Agent 独立检查
```

## 当前边界

- 执行器是 **WorkBuddy 安装包中的 CLI**，通过 ACP 的标准输入和输出通信，权限请求逐项交给主 Agent 处理。实际执行使用 CLI 自己的工具。
- **WorkBuddy 桌面端必须已运行并登录。** 网关通过官方随包 IPC 库调用 `wb.request/http.fetch`，由宿主注入账号鉴权。账号登录凭据留在宿主；网关不读取、解密或导出登录令牌。
- CLI 的模型请求经过带随机 nonce 的临时 `127.0.0.1` 转发；设计用途是当前独立 CLI 的账号请求，不提供面向其他应用的兼容模型 API。转发仅允许 **`POST /v2/chat/completions`，不带 query**；其他方法、路径与带 query 的请求返回 403，不进入账号请求通道。初始化模型目录的只读查询单独处理，不对 CLI 开放该路由。
- 需要其他模型协议路径的模型目前不支持，不静默切换模型或回退协议。模型目录从当前账号读取，不能根据列表可见推断该模型协议已适配、额度充足或调用免费。nonce 认证不能扩大任务授权。
- CLI 使用独立 profile，临时连接认证只放在子进程内存环境中，不改已有账号设置。
- CLI 使用独立会话；不保证出现在桌面聊天历史，也不保证继承桌面端已登录的连接器。需要连接器的任务必须另行实测。
- 不自动启动、重启或操作 WorkBuddy 桌面应用，不需要浏览器或鼠标键盘自动化。
- 主 Agent 的授权不能扩大给执行器。默认不跳过 WorkBuddy 权限检查；收到权限请求时应核对具体操作与原任务授权，再决定允许或拒绝。
- 工作目录用于任务路由，不是操作系统级文件隔离。请先在独立测试目录试用。
- 本项目不承诺免费。模型调用仍受 WorkBuddy 的登录、额度、计费和服务规则约束；网关不能把未知消耗当成零。
- `0.1.0` 为早期版本。自动化测试、真实执行、重启恢复和公开发布分别验收，详见 [验收记录](docs/ACCEPTANCE.md)。
- 原生宿主转发采用完整缓冲，单次请求约有 **50 秒**、请求和响应各约 **640 KiB** 的边界。长输出、大上下文或复杂长任务可能超限，需要单独实测；当前不能据此宣称长期无人值守可用。

## 环境与运行

需要 Python 3.11 或更新版本、可用的 Node.js，以及已安装、已运行并登录的 WorkBuddy。当前原生通路校验 Windows 命名管道；其他平台的自动化测试不代表原生账号通路已经实机验收。内置 CLI、IPC 库与 ACP 能力受 WorkBuddy 版本影响，升级后需要重新检查。

从仓库根目录运行，无需安装 Python 第三方运行依赖：

```powershell
python -m buddygate doctor
python -m buddygate status
```

配置使用环境变量，以下路径仅为示例，请换成自己的真实安装位置：

```powershell
$env:BUDDYGATE_NODE = 'C:\Program Files\nodejs\node.exe'
$env:WORKBUDDY_CLI_PATH = 'D:\Apps\WorkBuddy\resources\app.asar.unpacked\cli\bin\codebuddy'
```

| 变量 | 用途 |
| --- | --- |
| `BUDDYGATE_NODE` | Node.js 可执行文件路径；用于运行 WorkBuddy 内置 CLI |
| `WORKBUDDY_CLI_PATH` | WorkBuddy 内置 CLI 脚本的绝对路径 |
| `WORKBUDDY_HOME` | WorkBuddy 安装目录，供 CLI 路径发现使用；显式 CLI 路径优先 |
| `BUDDYGATE_STATE_DIR` | 私有状态目录；默认在 `%LOCALAPPDATA%\WorkBuddyManagerGateway\state`，与源码分离 |

配置文件、任务记录、执行回报和成果可能含有私人信息，不能提交到 GitHub。诊断结果也需检查后再分享。

## 派工与返工

先准备一个已存在的独立目录，放入任务需要的输入文件，再提交任务。`submit` 和 `rework` 默认自动启动隐藏的常驻 worker，无需另开终端。目标中写清允许读写的范围、输出文件和验收条件：

```powershell
python -m buddygate submit --workspace 'D:\GatewayDemo' --objective '读取 input.csv，按月份汇总金额，写入 summary.json。只改 summary.json；报告来源行数、计算方法和未解决问题。' --key 'demo-monthly-001'
```

保存返回的任务编号，用它恢复任务；不要因等待超时再次派同一个目标。相同请求使用稳定的 `--key`。

网关自身不重派提交结果不确定的业务目标。底层 CLI 仍可能对连接或网络异常自行恢复、重试；这不构成上游每个网络请求绝对只发生一次的保证。

worker 在空队列时每秒检查本地记录，不调用模型。它不会启动或重启 WorkBuddy 桌面窗口；只在有任务时运行内置 CLI 执行器。

```powershell
python -m buddygate get TASK_ID
python -m buddygate events TASK_ID
python -m buddygate rework TASK_ID --feedback '在原结果中补充排除的无效记录数，其余字段保持不变。'
```

需要手动控制 worker 时，给 `submit` 或 `rework` 加 `--no-start`，再自行运行 `worker`。`worker --once` 只处理一个可执行轮次：

```powershell
python -m buddygate submit --workspace 'D:\GatewayDemo' --objective '只读检查输入文件，报告记录数。' --key 'demo-readonly-001' --no-start
python -m buddygate worker --once
```

查看任务计数与执行器发现信息，或停止空闲 worker：

```powershell
python -m buddygate status
python -m buddygate stop
```

`stop` 在仍有排队或活动任务时拒绝执行。允许停止后，空闲 worker 在约一秒内退出，不影响 WorkBuddy 桌面应用。`status` 的任务计数不等于对底层会话和外部操作的停止证明。

执行器回报完成后，主 Agent 应读取实际文件、检查必要来源或测试，再保存验收记录：

```powershell
python -m buddygate accept TASK_ID --note '已检查实际输出' --evidence '逐项对照输入重新核算，月份金额与总额一致；无额外修改。'
```

`accept` 记录检查结论，不替主 Agent 自动核对业务内容。任务成功受理、协议握手或文件非空，都不足以证明任务完成。

## 权限与取消

出现权限请求时，通过任务状态查看具体请求、`PERMISSION_ID` 与原生选项。请求ID和 `OPTION_ID` 都必须使用当前实际返回的值，不猜名称；过期请求不能继续审批。记录同意或拒绝的依据：

```powershell
python -m buddygate approve TASK_ID --permission-id PERMISSION_ID --option OPTION_ID --note '已核对请求操作与本次授权范围。'
python -m buddygate cancel TASK_ID
```

请求取消不等于所有已经发生的动作已撤回。检查任务状态和实际成果后再安排下一轮。提交结果不确定或执行器中断时，先恢复原任务并核查已有文件，不自动重放。

只有人工检查原进程、会话及相关外部动作，确认已停止后，才可处理 `uncertain` 状态：

```powershell
python -m buddygate resolve TASK_ID --note '已检查原执行器并确认停止，保留现有成果待核对。' --evidence '填写实际检查的进程、会话和成果证据。'
```

`resolve` 把 `uncertain` 改为 `blocked`，记录主 Agent 的说明与证据。网关不能验证这些陈述是否真实，也不自动恢复执行或回滚外部动作；需要继续时应在核查后明确返工。

## 接入主 Agent

MCP 使用本地 stdio 入口。宿主可能从任意工作目录启动进程，因此使用 `gateway.py` 的实际绝对路径，以下路径仅为示例：

```powershell
python 'D:\Tools\workbuddy-manager-gateway\gateway.py' mcp
```

在宿主的本地 MCP 配置中，把 Python 实际绝对路径作为启动程序，把 `gateway.py` 实际绝对路径和 `mcp` 作为两个参数；不依赖新聊天的工作目录或 PATH。MCP 的提交与返工工具默认自动启动隐藏 worker，不必另外手动启动。

本项目不会自动改写所有客户端的全局配置。只给需要的宿主注册，保留已有 MCP 配置。

## 开发检查

```powershell
python -m unittest discover -s tests -v
```

CI 配置覆盖 Windows、Ubuntu 的 Python 3.11 与 3.12 标准库测试，实际结果以对应提交的工作流记录为准。模拟测试不证明本机 WorkBuddy 已经连接，真实验收另见 [ACCEPTANCE.md](docs/ACCEPTANCE.md)。

同类方案和选择依据见 [COMPARISON.md](docs/COMPARISON.md)，参考与实现说明见 [NOTICE.md](NOTICE.md)。本项目采用 [MIT License](LICENSE)，与 WorkBuddy 官方没有隶属或背书关系。

## 公开发布

公开文件白名单见 [PUBLISH_MANIFEST.txt](docs/PUBLISH_MANIFEST.txt)，每行一个仓库相对路径。打包或上传前检查这些文件的实际内容，并核对清单已涵盖最终源文件与测试。不要直接压缩整个工作目录；缓存、状态、任务日志、凭据和用户成果不属于发布内容。`.gitignore` 只用于防止误提交，不能替代内容检查。

在仓库根目录生成白名单源码包与 SHA-256 文件，输出目标须是尚不存在的新文件：

```powershell
python scripts/package.py --output '..\workbuddy-manager-gateway-0.1.0.zip'
```

`scripts/publish_to_github.py` 支持先检查白名单，不创建仓库或上传：

```powershell
python scripts/publish_to_github.py --owner VERIFIED_OWNER --repo workbuddy-manager-gateway --check
```

真实发布必须得到公开上传授权，并核实 `--owner` 对应的账号。**由主 Agent 通过本网关派工，让 WorkBuddy 实际运行发布脚本；主 Agent 只准备与核验，不代传后归属给 WorkBuddy。** 发布器只使用现成的非交互 Git 凭据，核对当前账号和公开目标；没有凭据或账号不符时应报告阻塞，不打开浏览器、不启动登录流程，也不改全局 Git 配置。

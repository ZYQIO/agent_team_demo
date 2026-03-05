# Claude Code Agent Teams 调研（截至 2026-03-01）

## 1. 调研目标

目标是把 Claude Code `agent teams` 的关键机制拆解成可在本地实现的工程能力，而不是做界面或宣传层面的复刻。

## 2. 一手来源

- Agent Teams 文档: https://code.claude.com/docs/en/agent-teams
- Subagents 文档: https://code.claude.com/docs/en/sub-agents
- CLI 参考: https://code.claude.com/docs/en/cli-reference
- 成本文档: https://code.claude.com/docs/en/costs
- 官方 Changelog: https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md

## 3. 功能拆解（官方能力）

### 3.1 架构角色

- 存在 `lead`（主会话）与多个 `teammate`（独立会话）。
- 每个 teammate 有独立上下文窗口，且能通过团队机制共享任务与消息。

### 3.2 协作机制

- 共享任务列表（任务状态、分配、依赖）。
- 任务领取后进入进行中状态，并通过锁避免重复执行。
- agent 间可发消息（邮箱模型），不仅仅是“子任务结果回传”。

### 3.3 执行模式

- 需要显式开启实验开关 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`。
- 支持 `in-process` 与 `tmux` 方式。
- 队员可并行执行，适合复杂、多分支问题。

### 3.4 与 subagents 的差异（关键）

- subagents 更像“主会话发起短期子任务，回传摘要”，上下文隔离更明显。
- agent teams 是“长期多会话协作”，队员之间也可直接通信和协同分工。

### 3.5 成本与限制

- 成本文档明确提到团队模式通常显著更耗 token（文档里给出约 7x 的数量级描述，取决于模式和任务）。
- 当前仍属于实验能力，文档列出了恢复、单会话 team 数量、嵌套 team、lead 转移等限制。

### 3.6 版本演进信号（来自 Changelog）

- `2.1.32` 引入了 agent teams research preview。
- 后续版本持续修复：tmux 消息传递、provider 环境透传、内存泄漏、批量中断行为等问题。
- 结论：功能可用但演进中，稳定性靠持续迭代。

## 4. 可复刻的最小核心（本地工程视角）

要做“类似能力”，最小要有：

1. **角色模型**：Lead + Teammates。
2. **共享任务板**：状态流转、依赖、领取锁。
3. **消息总线**：队员与 lead 的异步通信。
4. **并行执行器**：多 teammate 并发跑任务。
5. **冲突控制**：文件级锁（避免多人同时改同一文件）。
6. **可追溯日志**：任务与消息事件落盘。

## 5. 不做的部分（第一版范围外）

- 不复刻 Claude 内部模型调用与 UI 行为。
- 不复刻其账号、套餐、云端会话持久化体系。
- 不实现跨机器分布式部署（先单机 in-process）。

## 6. 对本仓库的落地判断

本仓库现有 `agent_team_demo.py` 已有“分角色”雏形，但缺失共享任务板、消息总线、锁与并发控制。  
因此可以在该目录直接升级为“可运行的 team runtime MVP”，用 Markdown 分析任务验证机制。

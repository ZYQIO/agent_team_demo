# Agent Team 功能开发计划（MVP）

## 1. 目标

在本地实现一个可运行的 `agent team` MVP，具备：

- Lead + 多 Teammate 协作
- 共享任务板（依赖、状态、领取锁）
- agent 邮箱通信
- 并发执行
- 文件锁防冲突
- 可追溯日志与最终报告产物

## 2. 范围

### In Scope

- Python 单进程多线程实现（in-process）
- 面向 Markdown 任务链的示例 handlers
- 命令行运行入口与输出目录产物

### Out of Scope

- 跨进程/跨机器分布式
- GUI
- 外部 LLM API 编排

## 3. 架构设计

### 3.1 核心模块

1. `TaskBoard`
- 维护任务状态：`pending / in_progress / blocked / completed / failed`
- 维护任务依赖与领取机制（原子 claim）

2. `Mailbox`
- 支持 `send(from, to, subject, body)`
- 支持 `broadcast(from, subject, body)`
- 支持 agent 拉取 inbox

3. `FileLockRegistry`
- 路径级锁，避免并发写冲突
- 任务启动时申请、结束时释放

4. `LeadAgent`
- 初始化任务图
- 监听队员消息
- 汇总结果与产物

5. `TeammateAgent`
- 按技能声明领取任务
- 执行 handler，回写结果
- 遇错上报

### 3.2 任务流（MVP）

1. `discover_markdown`  
2. `heading_audit`（依赖 1）  
3. `length_audit`（依赖 1）  
4. `recommendation_pack`（依赖 2,3，写报告文件并加文件锁）

## 4. 里程碑

### M1: 数据结构与运行时骨架

- dataclass: Task / Message / AgentProfile
- 线程安全共享状态（锁）
- 事件日志器（jsonl）

### M2: 任务板 + 邮箱 + 锁

- Task claim/release/complete/fail
- inbox 拉取与消息路由
- 文件锁申请与释放

### M3: Team Runner 与 Markdown handlers

- 启动 lead + 3 teammates
- 跑完整任务图
- 输出 `final_report.md`、`task_board.json`、`events.jsonl`

### M4: 文档与验证

- README 更新运行方式
- 执行一次真实 run 并检查产物

## 5. 验收标准

1. 一条命令可跑通全流程，不人工介入。
2. 并发阶段至少出现两个 teammate 同时处理不同任务。
3. 任务依赖生效（依赖未完成不能领取）。
4. 报告文件成功落盘且有可读建议。
5. 失败任务可记录 error，不导致整体崩溃。

## 6. 风险与应对

1. 线程竞态
- 应对：共享数据统一通过 `threading.Lock` 保护。

2. 任务饿死或空转
- 应对：调度循环引入短 sleep + 终止条件检查。

3. 输出不可追踪
- 应对：关键事件统一写入 `events.jsonl`。

## 7. 实施顺序

1. 先写 runtime 脚本和核心类。
2. 接入 Markdown handlers。
3. 跑通并发与日志。
4. 更新 README 和示例命令。

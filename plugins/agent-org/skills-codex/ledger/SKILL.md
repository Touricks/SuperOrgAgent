---
name: ledger
description: 在 Codex 组织中读取飞书项目工作项，或由已绑定书记员登记任务包、更新文档和状态并核对回执。
---

# 飞书台账

在项目目录运行 `python3 -I .agent-org/bin/codex_runtime.py context` 核对角色。读取用 `python3 -I .agent-org/bin/ledger.py get|query|trace|status`；完整参数先看对应 `--help`。

正式写入只在已绑定并观察到 hook 的 pmo-scribe 会话执行。先不加 `--apply` 预览，再按用户已有授权执行。读 `../../skills/ledger/references/commands.md` 和 `structure.md`；其中 `.claude/agent-org/` 命令路径在 Codex 改为 `.agent-org/`，不调用 Claude 的会话命令。

- 需求包、送审包、决策包用 register；交付包、验收包用 reply，沿用原工作项。
- 同一文件登记中断使用 `--resume`，保留 packages/sent 的回执。文档创建结果未知时先查目标节点，找到后用 `--doc <原文档>`；确认未创建才用 `--doc new`。
- transition 写后必须回读状态；未达到质量验收条件不标已验收。创建节点使用 ensure-node，沿用已绑定 ID。
- 普通角色仅可用 ledger comment 写评论，标签从真实角色绑定产生，不用环境变量伪装书记员。
- 连续两次写入失败停止该写入链，保留报错、logid、已完成步骤，向请求方交回异常。

所有外部写入受已有任务范围约束。只报告回读确认的状态与链接；创建类型不是 CLI 的能力。

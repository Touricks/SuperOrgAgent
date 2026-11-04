---
name: ledger
description: 在 Codex 组织中读取台账、承接已授权的登记或代办任务、发布独立正文并核对回执。
---

# 飞书台账

在项目目录运行 `python3 -I .agent-org/bin/codex_runtime.py context` 核对角色。读取用 `python3 -I .agent-org/bin/ledger.py get|query|trace|status`；完整参数先看对应 `--help`。

当前分工以项目 `AGENTS.md` 为准。默认由书记员承接登记，明确委派可以代办，无需配置逐会话写权。普通内容任务先写草稿并请求登记，工具可用不自动授权发布。同一登记请求只由一个执行者处理，交接沿用原工作项、文档和回执。

工作项及任务包操作先不加 `--apply` 预览，再按已有任务授权执行。命令参数与字段结构参考 `../../skills/ledger/references/commands.md` 和 `structure.md`；其中 `.claude/agent-org/` 改为 `.agent-org/`。共享材料里的 Claude 角色权限条款不作为 Codex 运行规则。

独立正文使用飞书文档工具，执行 `references/publishing.md`；已初始化项目也保存了当前版本的 `.agent-org/operations/publishing.md`。先核对目标空间、父节点和已有对象，再发布与回读，保存原生 ID 和正式链接，关联原任务。直接文档操作不自动享有台账锁和回执。

- 需求包、送审包、决策包用 register；交付包、验收包用 reply，沿用原工作项。
- 同一文件登记中断使用 `--resume`，保留 packages/sent 的回执。文档创建结果未知时先查目标节点，找到后用 `--doc <原文档>`；确认未创建才用 `--doc new`。
- transition 写后必须回读状态；未达到质量验收条件不标已验收。创建节点使用 ensure-node，沿用已绑定 ID。
- 评论标签从真实角色上下文产生，未绑定保持未知，不用环境变量伪装书记员。
- 连续两次写入失败停止该写入链，保留报错、logid、已完成步骤，向请求方交回异常。

所有外部写入受已有任务范围约束。只报告回读确认的状态与链接；创建类型不是 CLI 的能力。

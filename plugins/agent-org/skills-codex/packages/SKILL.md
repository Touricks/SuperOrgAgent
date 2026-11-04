---
name: packages
description: 按智能体组织规范起草需求、交付、送审、验收和决策包；复用同一套包头、关系与验收规则。
---

先读项目 org/org.md 的包与关系表、当前工作项和主文档状态包。模板在 `.agent-org/templates/`，只选择适用的五类模板之一。详细原则、接口与例子按需读取 `../../skills/packages/references/` 下对应文件。

草稿写到 `packages/drafts/<序列>/`，先运行 `python3 -I .agent-org/bin/orgspec.py lint <草稿>`。交付和验收答复已有工作项，不另建同类项。执行关系必须给出来源工作项，质量结论由独立质量序列产生。跨序列、送审、决策经过 PMO；序列内包按既有派发图交接。

需要登记时通过 entry 流程给唯一书记员发登记请求，提供路径、已有工作项、期望状态和摘要，不复制整个任务包进消息。

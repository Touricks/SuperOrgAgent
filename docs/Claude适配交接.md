# Claude 适配交接

Codex 交接版本为 **0.5.1-codex.2**，分支为 `codex/desktop-adapter`，运行代码基线为 `2bb30dfcfed58609c4299bb56f9706e2f26cb900`。本次交接只补文档，Claude 适配由 Claude 继续完成。

后续 **0.5.1-codex.3** 仅缩短 Codex 会话标题为 `[项目名] <池位名>`，并明确已有聊天由负责项目初始化的会话更新。以下现场结果仍对应 codex.2，共用业务逻辑和 Claude 标题生成保持原样。

## 从已实测的 Codex 基线继续

独立现场已完成飞书项目、29 个知识骨架节点、21 个原生角色会话及最小立项的关联回读。首次从空目录配置在 303.27 秒时未就绪；用户确认 Hooks 信任后，复用现有空间和会话的补齐测试在 291.15 秒通过。首次五分钟目标仍未通过，两轮均未配置视图。详细范围与剩余问题见 [Codex 实测修复记录](Codex实测修复.md)。

本次没有修改 Codex 运行代码或版本，也没有将实测的 21 个会话重新创建。后续如修改共用脚本，应重新运行对应回归，并为新的运行行为记录新版本。

## 保留共用业务逻辑

组织规则在 `plugins/agent-org/orgspec/`，飞书台账和空间绑定由 `scripts/ledger.py`、`scripts/setup.py` 等共用脚本实现。五类包模板和参考资料仍由两端复用。

Claude 使用 `.claude-plugin/plugin.json` 和 `skills/`；Codex 使用 `.codex-plugin/plugin.json` 和 `skills-codex/`。当前 Codex 生成器以共用初始化计划为输入，再生成自己的角色、AGENTS 区块和 Hooks。平台会话管理和身份检查分别维护。

当前初始化入口会阻止两套运行配置写入同一个已初始化目录。Claude 验证使用独立项目，保留已有 Codex 项目的空间绑定、工作项、文档和回执。

## Claude 需要调整的流程

- 将模板维护从普通用户初始化中分离。新项目复用已维护的 `std_template`；缺少类型、字段或流程时报告具体缺项，由模板维护者处理。
- 保持零视图配置：初始化不创建、不补齐、不检查自定义视图，任何工具路径都不承担视图配置。Agent 通过台账条件查询读取状态。
- 明确 CLI 模式与桌面会话池的验收区别。21 份角色定义属于配置；桌面会话池还需核对真实会话及角色绑定。当前手工创建七个顶层会话的流程和“约二十分钟”的说明，需要在 Claude 中重新验证和优化。
- 统一初始化终点，纳入最小立项工作项、主文档和三处关联回读。完整研究访谈、业务派活和开发测试套件放到初始化之后。
- 若采用五分钟目标，完整记录前置检查、授权、Hooks 信任和最终回读耗时。复用已有对象后的补齐测试单独记录，不覆盖首次结果。
- 保留唯一书记员的正式写入机制，以及写入锁、登记回执和中断恢复。初始化授权和角色开局顺序应与真实客户端能力匹配。

主要入口为 `plugins/agent-org/skills/setup/SKILL.md`、该技能的 `references/`、`docs/使用说明.md`，以及 `scripts/setup.py` 的 Claude 分支。Codex 的 `skills-codex/`、`codex_runtime.py`、`codex_hook.py`、`codex_setup.py` 继续按现有适配维护。

## Codex 保留的后续事项

首次配置的主要阻塞是没有观察到实际 Hooks，知识节点和最小立项因此未能完成。下一轮应围绕首次信任引导、书记员启动顺序和实际耗时继续验证，保持 21 个会话与完整节点要求。

立项主文档另有“关联工作项：待登记”残留。模板使用“待建”，当前登记脚本只替换这一固定文字。后续修复应覆盖登记前校验或规范化、真实 ID 写入和既有正文的原位修正；工作项与文档的现有身份应沿用。

本轮只测试初始化。新版完整业务交接和跨企业模板获取仍需各自的现场证据。

## 修改后的验证

共用代码变更运行以下检查；纯文档修改核对事实、链接和差异即可。

```bash
python3 -I -B plugins/agent-org/scripts/orgspec.py check
python3 -I -B plugins/agent-org/tests/test_plugin.py
python3 -I -B plugins/agent-org/tests/test_codex.py
python3 -I -B plugins/agent-org/tests/test_initialization.py
```

Claude 的现场测试单独保存报告，区分安装、生成文件、实际会话、飞书回读和首次耗时。涉及共享运行代码的发布同时说明两端版本及验证范围。

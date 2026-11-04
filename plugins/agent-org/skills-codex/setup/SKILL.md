---
name: setup
description: 在 Codex Desktop 初始化、升级或检查智能体组织。复用已维护的飞书模板，在五分钟配置目标下生成项目角色、hooks 和知识库，不配置自定义视图。
---

# 初始化智能体组织

本技能的插件根目录是本文件的 `../../`。先解析为绝对路径，再执行其中的 scripts，不要猜测缓存路径。项目文件使用目标工作目录。

已有 `.agent-org/manifest.json` 的 Codex 项目走升级流程，不重复创建空间、目录、会话或立项单，不重启首次初始化计时：

```bash
python3 -I <插件根>/scripts/codex_upgrade.py upgrade --target <项目目录>
# 核对预览，在已授权范围执行
python3 -I <插件根>/scripts/codex_upgrade.py upgrade --target <项目目录> --apply
```

先暂停新的写入，保留任务与回执。升级只更新本地插件文件并保存可恢复备份；冲突生成 `.new`，合并后重新预览。中断用 `codex_upgrade.py recover --target <项目目录>` 恢复后重新预览；需要回退用 `rollback --target <项目目录>`，保留业务状态和远端对象。已有会话下一轮读取当前 AGENTS、角色和发布流程。运行 `setup.py check` 核对剩余问题；缺远端数据不自动重建。

以下步骤只用于新项目：

1. 从下载插件后开始配置时计时：`python3 -I <插件根>/scripts/codex_onboarding.py begin --target <目录>`。恢复同一轮沿用起点，认证、安装依赖、空间复制和 hook 信任等待都计入 300 秒。读目标项目 AGENTS.md、现有 `spec/task.json` 和初始化记录。已有 Claude 项目使用独立目录；任务格式见 `../../scaffold/task.codex.example.json`，使用 `runtime: codex`、`mode: pool`，模型留空继承用户配置。
2. 一次检查 `codex --version`、hooks/plugins 能力、`lark-cli --version`、`meegle version`、两边认证以及 list_projects、create_thread、list_threads、read_thread、wait_threads、send_message_to_thread。缺依赖或权限时记录具体缺项；不要通过预先完成这些步骤缩短首次计时。
3. 飞书项目复用已维护的 `std_template`。只读核对模板后，通过“分享空间配置”复制，按真实 project_key 明确选择新空间；已有授权范围内可操作复制页面。模板结构缺失交模板维护者，不在新用户初始化中搭建类型、字段或流程。**自定义视图完全排除：不创建、不补齐、不检查，不用 computer-use 配置飞书视图，也不因缺视图阻塞。** Agent 用台账条件查询工作项。具体绑定规则见 `../../skills/setup/references/feishu-space.md`。
4. 在已授权范围内创建或选定知识空间，回读真实 ID。运行 `python3 -I <插件根>/scripts/setup.py init --runtime codex --target <目录> --task <任务文件>`，核对预览后加 `--apply`。再用 `setup.py feishu --target <目录> --live` 预览并绑定，已授权时加 `--apply`。字段字典按类型限量并发读取。保留用户 AGENTS.md 和已有 hook；生成 `.new` 时先合并再重跑，不安装混合版本。
5. 如 Codex 要求在 Settings > Hooks 审阅并信任，说明所需操作并等待用户完成；不改全局信任记录。用户明确要求创建角色聊天后，按 `../entry/SKILL.md` 开池；只初始化本地文件不授权创建 21 个聊天。先让书记员开局，再分批创建其余角色，以便知识节点准备与其余角色开局并行。每批至多 4 个独立创建，预约、创建、绑定的依赖保持顺序。
6. 默认书记员或明确承接本次配置任务的主会话预览并执行 `.agent-org/bin/ledger.py ensure-node all`。完整七序列为 29 个节点；已有节点沿用 ID，同一父节点只列一次。按项目实际范围登记最小立项单，回读项目工作项和主文档。不要在配置阶段展开研究访谈、业务派活或完整测试套件。
7. 回读原生聊天的 projectId，全部保留在所属项目中，不设全局置顶。运行 `setup.py check --target <目录>`，检查 SessionStart、PreToolUse、Stop 和真实角色绑定；UserPromptSubmit 不作必需条件。完成飞书项目、知识库和会话的当次回读后运行 `codex_onboarding.py finish --target <目录>`，保存耗时与证据。脚本的 `timing_pass` 只表示本地检查通过且未超时；三处回读缺失仍不得宣称现场通过。

验收分开记录：三处配置和最小立项是否成功、首次配置是否在 300 秒内、后续业务交接是否通过。超时照实报告，不重置起点。完整派活、交付、质量验收及恢复测试在配置计时结束后进行，执行规则见 entry、ledger 技能。

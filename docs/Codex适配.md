# Codex Desktop 适配

版本：0.5.1-codex.4。基于上游 0.5.1，业务职责由项目 AGENTS.md 规定，会话绑定用于路由与追溯。正式工作项继续经过台账事务，独立正文使用文档工具。Claude 发布清单保持 0.5.1；本轮不执行 Claude 全量回归和全新目录五分钟实测。

## 安装与首次配置

在本仓库目录运行：

```bash
codex plugin marketplace add <本仓库绝对路径>
codex plugin list --marketplace agent-org-codex --available --json
codex plugin add agent-org@agent-org-codex
```

新开聊天确认 setup、entry、ledger、packages、charter、spec 六个技能可发现。源码、已安装缓存、目标项目中的生成文件是三份不同对象；更新源码后须通过正常安装流程更新插件，再对目标项目预览升级，不手改缓存。

首次配置目标为下载插件后 300 秒内完成飞书项目、知识库和默认 21 个角色聊天，并登记最小立项单。从开始配置时运行计时，不预先排除安装依赖、认证或人工等待：

```bash
python3 -I plugins/agent-org/scripts/codex_onboarding.py begin --target <目标项目>
```

复用已经维护好的模板。复制 `plugins/agent-org/scaffold/task.codex.example.json` 为任务文件，填写项目范围与真实空间标识；使用 `runtime: codex`、`mode: pool`，models 留空继承用户设置。预览与执行：

```bash
python3 -I plugins/agent-org/scripts/setup.py init --runtime codex --target <目标项目> --task <任务文件>
python3 -I plugins/agent-org/scripts/setup.py init --runtime codex --target <目标项目> --task <任务文件> --apply
```

具体操作顺序见 setup 技能。先准备两个飞书空间、生成本地文件并绑定；用户信任项目 hook 后优先开书记员，其余角色分批创建，每批至多 4 个独立请求。书记员建立知识节点时，其余角色可继续开局。依赖顺序保持为预约 → 创建 → 真实 ID 绑定 → 等待开局 → 原生回读。

生成内容包括：

| 路径 | 用途 |
|---|---|
| AGENTS.md 的 agent-org 区块 | 当前职责、委派与外部写入规则 |
| .codex/hooks.json | 角色上下文及工具检查 |
| .agent-org/roles/ | 角色职责 |
| .agent-org/pool-plan.json | 稳定池位、标题、模型和入口顺序 |
| .agent-org/state/pool.json | 会话绑定、预约、消息、聚焦和历史 |
| .agent-org/state/onboarding.json | 首次配置起点与历次结束检查 |
| .agent-org/bin/ | 适配脚本及共享台账实现 |
| .agent-org/operations/publishing.md | 当前版本的独立正文发布流程 |
| .agent-org/upgrades/ | 本地升级备份；业务记录不回退 |
| org/会话入口.md | 八个重点入口的查找顺序 |
| spec/、org/、packages/、feishu/ | 规格、组织、任务包和飞书绑定 |

用户维护的规格、决策和阶段计划保留；其他已修改生成文件产出 `.new`，本轮不安装混合版本，合并后重跑。原有 AGENTS.md 的其他内容和第三方 hook 保留。已有项目升级沿用空间、文档、回执和会话，不能覆盖旧项目来制造新用户测试结果。

## 已有项目升级

已有项目使用专用升级入口，不重新初始化、不重置计时、不创建空间、节点或会话：

```bash
python3 -I plugins/agent-org/scripts/codex_upgrade.py upgrade --target <已有项目>
python3 -I plugins/agent-org/scripts/codex_upgrade.py upgrade --target <已有项目> --apply
```

先结束正在执行的写入，再核对预览中的本地变更。角色、脚本与管理规则一起更新；AGENTS 管理块之外的约定和用户 hooks 保留。冲突生成 `.new` 后停止，合并再预览。安装期间持有与台账兼容的本地文件锁，先保存备份，最后写版本清单。

中断后执行 `codex_upgrade.py recover --target <已有项目>` 恢复完整旧版本，再预览升级。回退使用 `rollback --target <已有项目>`，可用 `--backup <备份ID>` 指定版本；存在新的文件修改时停止并保留现场。恢复和回退只修改备份中的插件文件，保留业务任务、远端对象和新回执。

已有聊天下一轮读取当前 AGENTS、角色说明和项目内发布流程。`context` 返回当前规则路径与哈希，不依赖聊天自动刷新旧规则。源码验收记录见 [Codex 写入规则验证记录](Codex写入规则验证记录.md)。

## 会话归属与入口

`setup.py pool --target <项目>` 只输出计划。用户明确请求创建角色聊天后，使用原生 list_projects 核对项目和主机，reserve 后调用 create_thread，再 bind 真实 threadId/hostId。若只返回 clientThreadId，保留预约等待核对；超时不直接重开，同名不是身份凭据。

标题使用 `[项目名] <池位名>`，例如 `[Thesis-Phase1] RnD-p0-01`。不追加中文序列名、重复职级或“池位”“入口”“路由”“书记员”；相关职责仍写在角色文件里。升级只生成新计划，已有聊天由负责该项目初始化的会话按真实绑定 ID 更新标题，保留原会话身份与用户自定义命名要求。

所有角色保留在所属项目，不自动全局置顶。`org/会话入口.md` 依次列出 PMO +2、PMO +1、产品 +2、算法 +2、工程 +2、基础设施 +2、质量 +2、外联 +2。当前原生工具不支持固定项目内部的聊天排序，这张表提供查找入口，不声称改变了侧栏顺序。已有用户置顶保持原样。

将 list_threads 的完整业务对象交给 `codex_runtime.py observe --file <文件>`；应包含目标会话、hostId、projectId 和 pinnedThreads。列表未覆盖全部角色时继续查询，不能把一页缺失当作不存在。`check` 检查真实项目归属，未置顶不报错。替换聊天后重做归属回读，待办沿用原池位，工作项和文档保留原 ID。

## 飞书项目与知识库

模板维护者预先配置类型、字段、状态、流转表单和角色，默认模板名为 std_template。新项目通过“分享空间配置”复制，再按实际 project_key 绑定。CLI 能创建已知类型的工作项实例，不支持据此新增工作项类型。缺模板或结构不齐应报告配置阻塞，不让新用户现场搭建模板。

**初始化不创建、不检查、不补齐自定义视图，禁止用 computer-use 配置飞书视图。** 2026-11-03 在模板中新增公开“阻塞”条件视图并加入空间置顶，复制到独立空间后查不到该视图；系统预置“全部”视图正常带入。该实验结果已用于移除视图初始化要求。空间复制页面仍可在授权范围内使用；它不包含视图搭建。

`setup.py template` 只读核对模板，`setup.py feishu --live` 预览绑定，加 `--apply` 保存。字段字典最多并发读取 4 种类型，单个请求失败不会保存不完整的导出。书记员运行 `ledger.py ensure-node all` 建立完整七序列的 29 个节点，一次运行按父节点共用列表结果，通常只需列出 4 组；已有 ID 沿用。

Agent 使用 queue、blocked、verifying、reviewing、open、recent 查询台账。默认书记员承接登记，明确委派可代办，无需增加会话权限。工作项与任务包通过 ledger.py 写入，保留写锁、回读、登记回执和 `--resume`。独立正文按 `.agent-org/operations/publishing.md` 使用文档工具发布，逐篇记录真实 ID 和回读证据，关联原任务。主文档标题改为“工作项 ID｜包名（作者）：任务名”，标题值不再以 `#` 开头，避免 Markdown 导出出现双标题符号。

## 消息、开工与任务边界

发送前登记 message，相同请求 key 复用原消息，结果未知先查原聊天。接收者先执行 `ack <消息ID>`，实际处理完再加 `--handled`。原生 create_thread/send_message_to_thread 未必触发 UserPromptSubmit，因此显式 ack 是必经步骤；queued、delivered 或一次 PreToolUse 不自动视为收到。

需求包和送审包分别声明业务产物与过程材料的写入范围。过程范围覆盖约定的草稿、交付、送审及证据目录；与原始限制冲突时由发出方澄清，执行角色不得自行扩大授权。

派活和送审必须具备正式登记回执及匹配的工作项主文档，message 会实时核对：

| 工作项 | 发送前 | 接收者开工前 |
|---|---|---|
| 同序列需求 | 执行中，负责会话与接收池位一致 | 仍为执行中，负责会话仍一致 |
| 跨序列需求 | 待接单或已接单 | 已接单 |
| 送审 | 待核查，来源项待验收 | 状态与来源项再次核对 |

接收者写入任何业务或过程材料前运行 `start <消息ID>`。此命令要求先 ack，再核对最新工作项状态及任务包摘要；派活后包内容变了，须由发出方核对并登记新消息。开工检查是流程保障，文件 hook 仍只负责明确的保护路径，不声称提供操作系统隔离。

received、handled 与工作项已验收分别记录。Stop 只表示回合结束；业务完成以质量结论、验收包和台账状态为准。

## hooks 与配置验收

在 Codex 要求时，由用户在 Settings > Hooks 审阅并信任生成的 hook；插件不修改信任记录。必需事件为 SessionStart、PreToolUse、Stop，每个角色必须观察到真实 PreToolUse。UserPromptSubmit 保留兼容，但缺失不使初始化失败。

apply_patch 解析支持原生 `tool_input.command`，并保留旧 input/patch 包装。新增、修改、删除、移动都按实际路径检查，符号链接不能绕过评测或配置保护。台账写入口独立检查生成代码和配置的完整性，不按会话角色拒绝写入。项目 AGENTS.md 可按明确要求维护；运行配置和评测路径继续受保护。任意脚本或未触发 hook 的工具不属于路径检查的覆盖范围。

完成当次三处回读和最小立项登记后运行：

```bash
python3 -I plugins/agent-org/scripts/setup.py check --target <目标项目>
python3 -I plugins/agent-org/scripts/codex_onboarding.py finish --target <目标项目>
```

`timing_pass` 只表示本地检查通过且总耗时不超过 300 秒，现场验收还需三处当次回读。计时不因中断重置，最终检查耗时也计入。缺依赖、认证、复制权限或超时都如实记为该轮首次配置未通过；不能先准备好后重跑计时，宣称“任何新用户五分钟可用”。

完整业务交接与恢复测试在配置计时结束后进行，见 [新项目测试提示词](Codex新项目测试提示词.md)。开发回归命令如下，用户初始化不执行整套回归：

```bash
python3 -I -B plugins/agent-org/scripts/orgspec.py check
python3 -I -B plugins/agent-org/tests/test_plugin.py
python3 -I -B plugins/agent-org/tests/test_codex.py
python3 -I -B plugins/agent-org/tests/test_codex_upgrade.py
python3 -I -B plugins/agent-org/tests/test_initialization.py
```

当前实测修复与五分钟验收状态见 [修复记录](Codex实测修复.md)。

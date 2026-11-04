# Codex Desktop 适配

版本：0.5.1-codex.1。基础版本：上游 0.5.1，包含方括号路径修复。Claude 的发布清单仍为 0.5.1，Codex 使用独立清单和技能入口。

## 使用方式

在本仓库目录运行：

```bash
codex plugin marketplace add <本仓库绝对路径>
codex plugin list --marketplace agent-org-codex --available --json
codex plugin add agent-org@agent-org-codex
```

新开 Codex 聊天确认 setup、entry、ledger、packages、charter、spec 六个技能可发现。安装插件与初始化研究项目是两步；不手改安装缓存。

复制 `plugins/agent-org/scaffold/task.codex.example.json` 为任务文件，填写项目名、范围和明确选定的飞书项目空间。在独立的目标项目中先预览，再执行：

```bash
python3 -I plugins/agent-org/scripts/setup.py init --runtime codex --target <目标项目> --task <任务文件>
python3 -I plugins/agent-org/scripts/setup.py init --runtime codex --target <目标项目> --task <任务文件> --apply
```

生成的内容：

| 路径 | 用途 |
|---|---|
| AGENTS.md 中的 agent-org 区块 | 所有会话共用的组织入口，不授予角色 |
| .codex/hooks.json | 角色上下文、消息接收和工具调用检查 |
| .agent-org/roles/ | 从同一份组织规则生成的角色职责 |
| .agent-org/pool-plan.json | 稳定池位、标题、模型继承和八个置顶入口 |
| .agent-org/state/pool.json | 实际会话绑定、创建预约、回执、聚焦和历史 |
| .agent-org/bin/ | 适配脚本及共享台账实现 |
| spec/、org/、packages/、feishu/ | 研究规格、组织分工、任务包及飞书绑定 |

已有 AGENTS.md 的其他内容和第三方 hook 保留。研究规格、决策和阶段计划保留用户写作内容；其他已修改生成文件会产出 `.new`，本次不安装混合版本。合并后重跑初始化。

Codex 开发版使用桌面会话池；不接受 cli 模式，也不把 Claude 的 opus/sonnet/haiku 别名复制给 Codex。models 留空时继承用户设置。默认 21 个角色聊天；pool.p1/p0/later 控制非核心池位数量，PMO 路由和唯一书记员必须存在。

## 会话与置顶

`setup.py pool --target <项目>` 只输出计划。用户请求开出会话后，setup/entry 技能使用 Codex 原生工具执行。先从 list_projects 核对项目 ID 和主机，再为每个池位 reserve，按返回提示调用 create_thread，最后 bind 真实 threadId/hostId。子会话可用一次性预约 claim；父子登记的先后不影响结果。

只返回 clientThreadId 或结果未知时保留预约并核对，不把它当 threadId，也不重复创建。同名不是身份凭据。重新初始化保留已有绑定。

默认置顶顺序为 PMO +2、PMO +1、产品 +2、算法 +2、工程 +2、基础设施 +2、质量 +2、外联 +2。只移动这些聊天到原生 pinned 区域，不改其他用户置顶项；已置顶项保留用户排序。完成后回读 list_threads 的完整 pinnedThreads，再用 observe 登记。check 会报告尚未回读的置顶项。

实际角色文件在 context 返回。没有绑定的普通聊天不承担组织角色，临时子智能体不继承父聊天的书记员写权限。替换聊天需要核对旧 ID，旧绑定进入 history；新聊天接续原池位的待办，工作项和文档不重建。新入口置顶后再取消旧入口置顶，保留旧聊天历史。

## 飞书项目与知识库

业务模型沿用原插件：模板空间人工预设工作项类型、字段与流程，每个项目从模板空间复制。`std_template` 是默认模板名；任务文件可指定实际模板空间。CLI 可以创建已知类型的工作项实例，不能据此推断它能创建新类型。

`setup.py template` 核对模板；`setup.py feishu --live` 预览目标空间，按明确授权加 `--apply` 保存绑定。空间换绑继续保留原有核对和备份机制。知识库按既有文档树组织；已绑定书记员通过 `.agent-org/bin/ledger.py ensure-node all` 建齐缺少的节点。

正式文档、工作项和状态仍只由书记员通过 ledger.py 写入；其他角色可以写带真实角色标签的评论。ledger.py 沿用写入锁、回读、packages/sent 登记回执及 `--resume`。交付包和验收包挂在原工作项下，只有需求、送审、决策等规则指定的主包创建工作项。

## 消息与验收

message 先记录意图再输出原生发送参数。相同内容重复调用返回 reuse；明确的新一轮业务消息可指定新的 `--key`，同一个 key 不允许改变内容。用户授权的会话通信范围内才调用 send_message_to_thread。

| 记录 | 能证明什么 |
|---|---|
| prepared | 本地已登记发送意图 |
| queued / delivered | 原生工具报告排队或投递 |
| received | 接收方 hook 看到了匹配的完整消息，或接收方显式确认 |
| handled | 接收方记录处理完成 |
| 工作项验收状态和验收包 | 业务验收结果 |

发送超时保持 unknown，先按消息 ID 查原生聊天。对方收到后，晚到的发送回执不会倒退状态。+2 聚焦时消息暂存，不主动打断；下一轮 context 会列出待办。Stop 只记 turn_ended，不改变飞书工作项。

## hook 与验证边界

在 Codex 的 Settings > Hooks 审阅并信任生成的 hook。插件不改信任记录。需要核对的事件是 SessionStart、UserPromptSubmit、PreToolUse 和 Stop；Interrupt/SessionEnd 保留退出状态。hook 的 session_id 必须与真实 threadId 对齐，不能从标题推断。

PreToolUse 检查书记员身份、已知 CLI 写命令、ledger 入口、apply_patch 的新增/修改/删除/移动路径和共享工作树切换。台账写入口还会独立检查角色绑定及已观察的 hook。shell 命令解析是防误操作措施，不是操作系统权限隔离；任意脚本、未触发 hook 的工具和同账号恶意程序不在这个保证内。不要让多个会话同时修改共享目录的同一文件。

```bash
python3 -I -B plugins/agent-org/scripts/orgspec.py check
python3 -I -B plugins/agent-org/tests/test_plugin.py
python3 -I -B plugins/agent-org/tests/test_codex.py
python3 -I plugins/agent-org/scripts/setup.py check --target <目标项目>
```

离线测试覆盖生成、保留用户文件、方括号路径、绑定幂等、排队创建、置顶回读、消息去重、聚焦、替换、书记员约束和子智能体身份隔离。测试中的会话与 hook 输入是夹具，不替代桌面现场验证。

现场验证使用单独测试项目：确认技能加载；信任 hook 后开出计划会话；回读八个置顶入口；用一组已授权消息核对发送和接收；由书记员登记测试需求包并回读工作项和文档；验证恢复仍沿用原 ID。没有这些证据时只报告已完成的本地检查。

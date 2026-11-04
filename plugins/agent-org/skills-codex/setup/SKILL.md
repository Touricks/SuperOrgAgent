---
name: setup
description: 在 Codex Desktop 初始化、升级或检查智能体组织。生成角色、会话池计划和 hooks，复用既有飞书模板空间及绑定流程。
---

# 初始化智能体组织

本技能的插件根目录是本文件的 `../../`。先解析为绝对路径，再执行其中的 scripts，不要猜测缓存路径。项目文件使用目标工作目录。

1. 读目标项目 AGENTS.md、现有 `spec/task.json` 和初始化记录。已有 Claude 项目不直接覆盖迁移；使用独立目录，保留原飞书绑定、文档和回执。任务格式见 `../../scaffold/task.codex.example.json`。Codex 使用 `runtime: codex`、`mode: pool`；模型留空继承用户配置。
2. 检查 `codex --version`、`codex features list` 中 hooks/plugins、`lark-cli --version`、`meegle version` 和认证状态。所需原生工具：list_projects、create_thread、list_threads、read_thread、wait_threads、send_message_to_thread、move_thread_to_sidebar_section。工具不在时明确缺项，不能把配置文件当作接通证明。
3. 运行 `python3 -I <插件根>/scripts/setup.py init --runtime codex --target <目录> --task <任务文件>`。核对预览；用户已授权初始化时加 `--apply`。用户改过的文件只生成 `.new`，本轮不升级其余文件；合并后重跑。不要覆盖用户 AGENTS.md 或已有 hook。
4. 飞书项目沿用人工配置的 `std_template`：核对模板、在飞书项目复制模板空间、明确选择目标空间，再用原有 `setup.py template` 和 `setup.py feishu --target <目录> --live` 预览绑定；执行已授权绑定时加 `--apply`。现有类型可创建实例；缺少类型时须补模板/空间配置，不能声称 CLI 能新建工作项类型。具体读取 `../../skills/setup/references/feishu-space.md`，其中涉及 Claude 会话的操作由下一步替换。
5. 知识库按 `org/org.md` 的文档树绑定。正式节点由已绑定书记员运行 `.agent-org/bin/ledger.py ensure-node all` 预览并执行；已有节点沿用 ID。需要创建知识空间或知识根时在用户明确的空间范围内操作，并记录到 `feishu/ids.json`，不虚构 ID。
6. 用户明确要求开出角色会话后，按 `../entry/SKILL.md` 的开池流程执行。只初始化本地文件不等于请求创建 21 个聊天。
7. 项目 hook 需要用户在 Settings > Hooks 审阅并信任，这是 Codex 的原生要求。不要修改全局信任记录或使用跳过信任参数。重新打开项目后用 `setup.py check --target <目录>` 检查真实 hook 记录、绑定和置顶；未通过就列出具体缺项。

汇报四种结果：本地文件、真实会话、飞书绑定与知识节点、现场登记验证。分别给证据，不合并宣称“全部完成”。

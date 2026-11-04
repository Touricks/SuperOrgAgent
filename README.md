# Super Org Agent

Claude Code / Codex Desktop 插件：把一个研究项目组织成七个序列（产品、算法、工程、基础设施、质量、项目管理、外联）、三个职级（+0 到 +2）的子智能体或桌面会话，研究员是 +3。项目管理 +2 是研究员平时的入口，特殊情况下研究员也直接委托其余六个序列的 +2；交接写成五类任务包（需求包、交付包、送审包、验收包、决策包）；任务账本在飞书项目，任务包在飞书文档。Claude 由书记员统一写入；Codex 默认由书记员承接登记，明确委派可由其他会话代办，具体职责见项目 AGENTS.md。

Claude 使用说明见 [docs/使用说明.md](docs/使用说明.md)。Codex 开发版基于 **0.5.1**，安装、初始化与验证见 [docs/Codex适配.md](docs/Codex适配.md)。

在新项目里试用 Codex 版，可直接使用 [Codex 新项目测试提示词](docs/Codex新项目测试提示词.md)。它覆盖飞书项目、知识库和角色会话的初始化，并通过一次真实交接检查三者的关联。

Codex 的 PMO 写入职责与独立正文发布问题已在 `0.5.1-codex.4` 修复，更新设计见 [Codex 写入规则修复方案](docs/Codex写入规则修复方案.md)，实施与验收状态见 [验证记录](docs/Codex写入规则验证记录.md)。

## 安装

```bash
claude plugin marketplace add <本仓库的路径或地址>
```

```bash
claude plugin install agent-org@agent-org
```

然后在要用的项目里初始化，见使用说明的“每个项目一次：初始化”。

## 目录

```text
.claude-plugin/marketplace.json      插件市场清单
.agents/plugins/marketplace.json    Codex 本地市场（agent-org-codex）
plugins/agent-org/
├─ .claude-plugin/plugin.json
├─ .codex-plugin/plugin.json         Codex 0.5.1-codex.4
├─ orgspec/                          组织规则（rules.json，英文标识）和术语表（terms.zh.json）
├─ scripts/                          orgspec.py 读规则和检查包；setup.py 初始化和开池清单；ledger.py 台账；route.py 路由、收件箱、聚焦会话和会话池；pool.py 认领和会话状态
├─ scaffold/                         初始化时生成项目文件用的模板和 hook
├─ skills-codex/                     Codex 的六个技能入口，共享业务规则和包模板
├─ skills/
│  ├─ setup/                         预检、初始化、搭模板空间、绑定飞书空间
│  ├─ charter/                       立项单、阶段计划
│  ├─ spec/                          访谈后写实验规格和评测协议
│  ├─ packages/                      五类包的模板、设计原则、序列接口、示例
│  ├─ ledger/                        台账读写和结构
│  └─ entry/                         路由决策表、决策队列、聚焦会话
└─ tests/test_plugin.py              不连飞书的测试
docs/
└─ 使用说明.md
```

## 规则只存一份

职级、序列、五类包、关系矩阵、交接规则、路由决策表、台账结构都在 `orgspec/rules.json`，用不变的英文标识；中文名在 `orgspec/terms.zh.json`。agent 定义、CLAUDE.md、组织规则全文和台账结构说明都由它生成，测试会检查生成物和它一致。改名只改术语表。

```bash
python3 -I plugins/agent-org/scripts/orgspec.py check
```

```bash
python3 -I -B plugins/agent-org/tests/test_plugin.py
python3 -I -B plugins/agent-org/tests/test_codex.py
python3 -I -B plugins/agent-org/tests/test_codex_upgrade.py
python3 -I -B plugins/agent-org/tests/test_initialization.py
```

## 状态

0.5.1-codex.4：项目 AGENTS.md 管理职责和委派，取消逐角色写入限制；工作项保留台账事务，独立正文通过文档工具发布。新增已有项目的本地升级、恢复和回退入口，保留会话与业务记录。83 个离线测试通过；PMO +2 已在既有项目完成升级和两篇正文发布、回读及原任务关联，21 个绑定和原登记记录保留。本轮五分钟首次初始化与 Claude 全量回归按用户要求另行验收，证据见[验证记录](docs/Codex写入规则验证记录.md)。

0.5.1-codex.3：会话标题缩短为 `[项目名] <池位名>`，例如 `[Thesis-Phase1] RnD-p0-01`，移除中文序列名、重复职级和“池位”等后缀。角色绑定和 Claude 命名逻辑保持不变；已有聊天由负责项目初始化的会话按新计划更新标题。

0.5.1-codex.2：修复实测中的原生 apply_patch 误拦截、消息收到回执、开工状态顺序、任务写入范围及文档标题问题。角色聊天保留在所属项目，八个重点入口写入 `org/会话入口.md`，不再自动全局置顶。模板新增公开条件视图后的复制实测未带出自定义视图，因此初始化不创建、不检查视图，禁止用 computer-use 配置视图。新增首次配置 300 秒计时，字段读取限量并发、知识节点按父节点复用列表。修复与后续独立实测见 [Codex 实测修复记录](docs/Codex实测修复.md)。

0.5.1-codex.2 的独立实测已完成三处初始化与最小立项回读。首次从空目录开始，303.27 秒时仍未就绪；用户信任 Hooks 后，复用空间和 21 个角色会话的补齐测试耗时 291.15 秒，通过必需配置检查。首次五分钟目标和立项正文占位文字仍列为后续问题。该版本的实测基线保留，Claude 侧流程调整见 [Claude 适配交接](docs/Claude适配交接.md)。

0.5.1-codex.1：首个 Codex Desktop 适配版。`runtime: codex`、`mode: pool` 生成 `.agent-org/`、项目 AGENTS.md 和 `.codex/hooks.json`，长期角色绑定原生会话 ID。创建预约、消息回执、聚焦与替换保留在本地；台账复用飞书模板、绑定、锁和登记回执。此版的全局置顶与 UserPromptSubmit 验收要求已由 codex.2 替代。

0.5.1：项目路径里有方括号这类通配符时（比如 `Phase1[Current]`），会话池读不到各会话的状态和登记回执：`route.py pool` 把所有会话显示为“未知”、派活前找不到空闲会话，每条【派活】都被当成没有登记回执，`check` 误报会话停在旧版规则上。现在按字面读这些目录。测试在项目目录或插件安装目录里运行时，不再读到目录外的文件而误报。

0.5.0：加了会话池模式（任务文件写 `"mode": "pool"`）。每个角色是研究员在桌面应用侧栏里看得见的一个会话：研究员新建七个 +2 会话并粘贴开局句，hook 按开局句认领，各 +2 开出本序列的 +1、+0 并预登记，子会话认领后由 hook 注入角色说明；派活、交回、登记都是会话之间带抬头的消息，`route.py pool` 找空闲的会话，`--watch` 看进度，`--recover` 在重启后续上。hook 只拦会损坏数据的事（书记员独写飞书、评测协议只读、high-risk 命令、`.claude/` 配置和认领表、派发带 name、共享目录里切分支），其余派发规则改成规范加审计，两种模式都一样；`ledger.py --apply` 加了文件锁。`setup.py pool` 打印开池清单，`check` 报认领表的问题。默认仍是 CLI 模式。

0.4.0：初始化把飞书文档这一边也算进来。setup 技能第四步加了“飞书文档”一节：研究员确认后建知识空间，再用 `ledger.py ensure-node all` 一次建齐节点骨架；`setup.py check` 会报项目空间没绑定、没有知识空间或节点不齐，交给用户之前的检查也加了这一项。

0.3.0：换空间有了标准做法。绑定每次按任务文件写的空间去飞书查，按 key 认空间，同名的停下；换空间时预览单独列出新旧空间，旧空间还有工作项的要研究员确认、加 `--leave-old-items` 才写入，`ids.json` 记下旧空间。没绑上任何类型的旧记录不再当成已绑定。步骤见 setup 技能 `feishu-space.md` 的“换空间”一节。

0.2.2：搭建手册写定了各类型详情页要放的字段；绑定预览里“网页上还要做的事”指向这一步。

0.2.1：搭建手册写清了流转表单怎么配、保存以后怎么核对必填，补了在飞书桌面端用真实鼠标拖放字段的做法。

0.2.0：飞书空间分两层。模板空间 std_template 每个实验室或企业搭一次，`setup.py template --live` 核对它；每个项目从模板复制出自己的空间再绑定。网页上的步骤见 setup 技能的搭建手册。

其余同 0.1.0 初稿：规则、模板、初始化、台账和路由脚本、测试都已齐；台账的主要写法和飞书私聊监听已在测试项上跑通；多层派发、聚焦会话、收件箱还没在真实会话里跑过，清单见使用说明的“还没实测的部分”。

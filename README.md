# Super Org Agent

Claude Code 插件：把一个研究项目组织成七个序列（产品、算法、工程、基础设施、质量、项目管理、外联）、三个职级（+0 到 +2）的子智能体，研究员是 +3。项目管理 +2 是研究员唯一的入口；交接写成五类任务包（需求包、交付包、送审包、验收包、决策包）；任务账本在飞书项目，任务包在飞书文档，书记员是唯一的写入方。

使用说明见 [docs/使用说明.md](docs/使用说明.md)。

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
plugins/agent-org/
├─ .claude-plugin/plugin.json
├─ orgspec/                          组织规则（rules.json，英文标识）和术语表（terms.zh.json）
├─ scripts/                          orgspec.py 读规则和检查包；setup.py 初始化；ledger.py 台账；route.py 路由、收件箱、聚焦会话
├─ scaffold/                         初始化时生成项目文件用的模板和 hook
├─ skills/
│  ├─ setup/                         预检、初始化、绑定飞书空间
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
```

## 状态

0.1.0 是初稿：规则、模板、初始化、台账和路由脚本、测试都已齐；台账的主要写法和飞书私聊监听已在测试项上跑通；多层派发、聚焦会话、收件箱还没在真实会话里跑过，清单见使用说明的“还没实测的部分”。

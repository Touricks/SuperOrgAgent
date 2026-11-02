---
name: setup
description: 在一个项目目录里初始化智能体组织：预检本机的飞书客户端、lark-cli、meegle、Claude Code 和 Python；生成七个序列三个职级的子智能体、入口 pmo-p2、项目权限和 hook、CLAUDE.md、org/、packages/、inbox/、spec/；把插件规则绑定到项目指定的飞书项目空间，列出空间里还要建或要核对的东西。用户说“初始化组织”“部署到这个目录”“在新电脑上装起来”“检查部署有没有漂移”“把飞书空间接上”“绑定空间”时使用。不替用户开通飞书权限，不改飞书项目的空间配置。
---

# 初始化智能体组织

插件装一次，每个项目再初始化一次。插件里的 agent 定义不认 hooks 和 permissionMode，权限只能写进项目 settings，项目目录要在项目里现场生成，飞书空间也要按项目绑定，所以这一步省不掉。

脚本在插件根目录的 `scripts/setup.py`（本技能目录往上两级）。下面的命令里 `$PLUGIN` 指插件根目录。

## 第一步：预检

```bash
python3 -I "$PLUGIN/scripts/setup.py" precheck
```

查飞书客户端、lark-cli 和 meegle 的版本和登录、飞书项目权限、meegle 诊断上报、飞书私聊的事件订阅、Claude Code 和 Python 的版本。有失败项先解决：登录、升级、关诊断上报都要用户本人在终端里做，把命令给用户，不要代跑。各项的含义和处理办法见 `references/precheck.md`。

## 第二步：写任务文件

任务文件只放脚本和 hook 要用的几项，不写研究内容。和用户一起写：先看代码库的目录，提出建议，再请用户确认。

- `name`、`description`：项目名和一句话简介。
- `editable_paths`：可改范围的可写路径，只有工程序列能写。
- `protected_paths`：评测协议用到的只读路径，比如评测脚本和评测集。
- `feishu`：`meegle_space` 必填，写这个项目用的飞书项目空间，空间名、simple_name 或 project_key 都行，没有它 `init` 会停下；`wiki_space` 写知识空间名。还没有空间的，请用户先在网页上新建一个，用户要是空间管理员。
- `models`：各角色用的模型，不写就用默认值。

格式照 `$PLUGIN/scaffold/task.example.json`，路径写项目内的相对路径。放在项目目录以外的评测数据不写进任务文件，设计阶段写进评测协议的“存放和版本”。研究问题、目标和判据在立项时写进立项单；实验规格和评测协议在设计阶段由算法 +2、质量 +2 用 `agent-org:spec` 访谈用户后写。旧版任务文件里的 x、f、g、h 和 search 照样认：路径照用，一句话说明只在第一次生成 `spec/` 下的文档时填进去。

## 第三步：初始化项目目录

```bash
python3 -I "$PLUGIN/scripts/setup.py" init --target <项目目录> --task <task.json>
python3 -I "$PLUGIN/scripts/setup.py" init --target <项目目录> --task <task.json> --apply
```

- 不加 `--apply` 只预览：列出要建的文件、要更新的文件和差异。
- 已有文件分三种处理：上次生成后没人改过的直接更新；本地改过的，新版本写在旁边的 `.new`，由用户对照合并；`org/decisions.md`、`org/phases.md` 和 `spec/` 下除 `task.json` 以外的文档由研究员维护，建好以后不再动；`spec/task.json` 跟着 `--task` 给的任务文件走。
- 同样的输入重复运行，报告“已写入 0 处”。
- `--sequences product,alg,quality,pmo` 只启用部分序列；项目管理和质量必须启用。

生成了什么、每个文件对应组织设计里的哪条规则，见 `references/mechanisms.md`。初始化完会提醒项目还没绑定飞书空间，接着做第四步。

## 第四步：绑定飞书空间

插件只写设计：工作项类型、字段和选项、状态和流转、流转表单、角色、视图，用中文名和英文标识描述，不带任何空间的 ID。每个项目把这套设计绑定到 `spec/task.json` 写的空间，绑定一次，把空间里的 type_key、模板 ID 和 field_key 记进 `feishu/ids.json`；之后台账脚本只按这些键读写，空间里改名也不会断。没绑定以前，台账脚本不读也不写飞书。

```bash
python3 -I "$PLUGIN/scripts/setup.py" feishu --target <项目目录> --live           # 预览：读空间、匹配，不写文件
python3 -I "$PLUGIN/scripts/setup.py" feishu --target <项目目录> --live --apply   # 核对以后写入
```

脚本按空间名精确找到空间，导出字段字典，再逐项匹配：先看 `--map` 指认的，再看上次绑定的键还在不在，最后按名字找。字段只认自定义字段（field_key 以 `field_` 开头），核对字段类型和选项，同名的不自动选。它还读回模板 ID、研究员的 open_id 和知识空间的 space_id，按名字查六个条件视图在不在。全是读操作；可选的几项读不到只提醒，不中断。

预览会打印 `feishu/migration.md`，即建空间与核对清单。和用户一节一节过：

- “要处理的问题”每条都写了怎么办。空间里已经有、只是名字不同的，不用改名，用 `--map` 指认：类型写 `--map <类型标识>=<空间里的类型名>`，字段写 `--map <类型标识>.<字段标识>=<空间里的字段名或 field_key>`，标识见清单各节的标题和表格。`--map` 可以写多个。指认一次就够：写入以后键记在 `feishu/ids.json` 里，之后不带 `--map` 也沿用，键在空间里没了才要重新指认。
- 空间里没有的，照“每种类型要有的东西”“条件视图”两节在网页上建，请用户自己做，建好重跑预览。
- “知识库节点”让书记员用 `ledger.py ensure-node` 建，先预览再 `--apply`。它只认标题完全相同的节点：知识空间里已有、只是名字不同的，先请用户在网页上改名。
- “读回”“网页上还要做的事”两节照表补。

问题都处理了，或者剩下的已经告诉用户，加 `--apply` 写入 `feishu/ids.json`（带绑定时间 `bound_at`）、两份字段字典和 `migration.md`。还有没绑上的类型或字段也可以先写入：用到它们的台账命令会停下报错，绑上以后就能用。以后空间有改动，或者任务文件换了空间，重跑一次 `feishu --live --apply`；任务文件写的空间和绑定的不一致时，台账脚本会停下。最后重跑一次 `init --apply`，让 CLAUDE.md 和书记员拿到新的 ID。

在网页上怎样建、实测过的坑，见 `references/feishu-space.md`。

## 第五步：检查

```bash
python3 -I "$PLUGIN/scripts/setup.py" check --target <项目目录>
python3 -I -B "$PLUGIN/tests/test_plugin.py"
```

`check` 用现在的插件按项目里的 `spec/task.json` 重新生成一遍，和初始化时逐个文件比对：列出插件、规则、术语表或 `feishu/ids.json` 变了以后要更新的文件、本地改过的文件和没合并的 `.new`；手改过 `spec/task.json`、lark-cli 风险表要换的，各自单独报。测试不连飞书：规则和包检查、初始化和漂移检查、真 hook 进程的拦截、按录下的飞书输出解析、用替身命令跑登记、答复、续做，setup 的读回和绑定，以及路由和私聊监听；真实写入见使用说明的“还没实测的部分”。

## 交给用户之前

- 预检没有失败项，或者失败项已经告诉用户怎么处理。
- `init --apply` 重跑一次报告“已写入 0 处”。
- 绑定已经 `--apply` 写入；`feishu --live` 报告“绑定：没有问题”，或者剩下的问题已经告诉用户怎么处理。
- `org/decisions.md` 开头列出了插件定下的几处安排，请研究员逐条确认或推翻。
- 告诉用户怎么开始：在项目目录里运行 `claude`，主会话就是入口 pmo-p2；第一件事是用技能 `agent-org:charter` 立项。

## 不做的事

- 不开通飞书权限，不登录、不升级命令行工具、不改 meegle 的诊断上报设置。
- 不改飞书项目的空间配置，不删任何飞书内容。
- 不替导师开飞书权限。

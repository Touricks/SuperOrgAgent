# ledger.py 命令

脚本在项目的 `.claude/agent-org/bin/ledger.py`，插件里的原件在 `scripts/ledger.py`。它读三样东西：规则文件（类型、字段、流转、文档放哪），`feishu/ids.json`（project_key、类型的 type_key 和模板 ID、知识库节点），`feishu/field-dictionary.json`（选项 ID）。

## 三种模式

| 模式 | 怎么进入 | 读 | 写 |
| --- | --- | --- | --- |
| 预览 | 默认 | 真读飞书 | 只打印命令 |
| 执行 | 加 `--apply` | 真读 | 真写，记 `feishu/write-log.jsonl`；流转写完回读状态，登记写完核对角色人员 |
| 离线 | 加 `--offline`（放在子命令前） | 不读 | 只打印；用于测试和没网时核对 |

## 它生成的底层命令

标“实测”的写法在真实空间上跑通过；“输出已核对”的，是在真实空间上用只读命令录下输出、解析代码按它改过（录下的输出在 `tests/fixtures/feishu/`）；标“未实测”的写操作，第一次用时先预览，再在测试项上执行。

| 用途 | 命令 | 状态 |
| --- | --- | --- |
| 建工作项 | `meegle workitem create --work-item-type <绑定的 api_name> --fields '<JSON>'`；`field_value` 一律是字符串；模板 ID 必填，用绑定时读回的；带 `role_owners`，把研究员填进“研究员”角色；建完读默认的 `workitem get`，看“当前负责人”（current_status_operator）里有没有研究员的 user_key，没有就 `workitem update --role-operate` 补；读不到这个字段时只提醒，不算成功 | 实测：带角色人员建、回读当前负责人都跑过；`--role-operate` 补写没用上，未实测 |
| 改字段 | `meegle workitem update --work-item-id <ID> --fields '<JSON>'` | 实测 |
| 读工作项 | `meegle workitem get --work-item-id <ID> --fields '["_all"]'`：类型在 `work_item_attribute.work_item_type.key`（type_key），状态在 `work_item_status.name`，名称在 `work_item_name`，字段在 `work_item_fields[].key/value`；没填值的字段不出现。人员字段（决定人）填了也读不回来，`get` 和流转再用一条 MQL 按工作项 ID 读，只取名字 | 输出已核对；人员字段读不回来是写入测试发现的 |
| 查 | `meegle workitem query --project-key <key> --mql '<MQL>'`：结果在 `data.<分组>[].moql_field_list`，状态是 `key_label_value_list`，人员是 `user_value`（name_cn、name_en、user_key、email，脚本只取名字）；总数在 `list[0].count`，空结果是 `data: {}`、`list: null`。多于 50 项时带首查的 `--session-id` 和 `--group-pagination-list '[{"group_id":"1","page_num":N}]'` 翻页，翻过头各项都是 null；最多读 20 页。其他形状一律停下报错 | 实测，输出已核对（含空结果和翻页） |
| 流转 | `meegle workflow list-state-transitions --work-item-id <ID>` 列出 `transition[]`，每项有 `id`、`state_name` 和 `confirm_form`（表单字段的 field_key）；必填项以 `confirm_form` 为准；先 `workitem update` 补表单字段，再 `meegle workflow transition-state --work-item-id <ID> --transition-id <id>` | 实测：不带表单的、带表单的都跑过；当前负责人为空也能流转 |
| 评论 | `meegle comment add --work-item-id <ID> --content '<文本>'` | 实测 |
| 上传主文档 | `lark-cli docs +create --parent-token <节点> --title '<标题>' --doc-format markdown --content - --as user`，正文走标准输入；返回只有 docx 链接和 document_id，再用 `lark-cli wiki +node-get --node-token <document_id> --as user` 换出 node_token，主文档字段存 `/wiki/<node_token>` | 实测：标准输入的写法、换节点都跑过 |
| 追加交接记录 | `lark-cli docs +update --doc <链接> --command append --doc-format markdown --content - --as user` | 实测 |
| 替换状态包 | `docs +fetch --scope outline --detail with-ids` 的 `data.document.content` 里找“状态包”“交接记录”两个标题块；再取这一段（`--scope range`），结束块取“交接记录”前最后一个顶层块，列表取它最后一个 `<li>`；然后 `docs +update --command block_replace --start-block-id --end-block-id --content -` | 实测：只换掉“状态包”到“风险”那一段 |
| 移动子树 | `lark-cli wiki +move --node-token <节点> --target-parent-token <新父节点> --as user` | 实测：移动后 node_token 和原链接都不变 |
| 建节点 | 先 `lark-cli wiki +node-list --space-id <ID> [--parent-node-token <父节点>] --page-all --as user`（结果在 `data.nodes[]`，有 `node_token`、`title`），上一级下已有同名节点就直接用；没有才 `lark-cli wiki +node-create --space-id <ID> --parent-node-token <父节点> --obj-type docx --node-type origin --title <标题> --as user` | 建节点实测；查同名的读法在空间上用预览核对过 |

lark-cli 只读当前目录、`/tmp`、`~/files` 下的 `@文件`，所以 `ledger.py` 不落临时文件，正文都从标准输入传。

## MQL

```sql
SELECT `work_item_id`, `name`, `work_item_status` FROM `<project_key>`.`<type_key>` WHERE `work_item_status` = '待决定'
```

- 表名用 type_key，或者类型现在的中文名；api_name 不行。`ledger.py` 用绑定时记进 `feishu/ids.json` 的 type_key；类型还没绑定的，停下报错。
- 每个状态流都自带“已终止”，视图默认不显示，MQL 不会自动排除。按“状态等于某个值”查的（queue、blocked、verifying、reviewing）本来就不含它；其余查询（open、cross、recent、source:<ID>）都加了 `NOT IN (..., '已终止')`。时间条件和 NOT IN 用 AND 连起来的写法（recent）在空间里跑过。
- 时间条件：``RELATIVE_DATETIME_BETWEEN(`updated_at`, 'past', '7d')`` 是近 7 天，``RELATIVE_DATETIME_BETWEEN(`updated_at`, 'current_week')`` 是本周。
- 来源工作项是单行文本，查“由谁引出”用 `= '<ID>'`；转成的工作项存多个 ID，用 `LIKE '%<ID>%'`。
- 单次最多 50 条；`ledger.py query` 会按返回的 session_id 自动翻页，手写查询时照上表的翻页参数取。
- 在 shell 里手写 MQL 时用带引号的 heredoc 传入，免得反引号被解释。

## 出错时

| 报错 | 原因 | 怎么办 |
| --- | --- | --- |
| 草稿没通过检查 | 包头缺项、栏目不对、用了旧包名 | 退回作者，附检查输出 |
| 这个项目还没绑定飞书空间 | 还没跑过 `setup.py feishu --live --apply` | 交回请求方，请研究员按 `agent-org:setup` 的第四步绑定 |
| spec/task.json 里的飞书项目空间是“…”，绑定的是“…” | 任务文件换了空间，还没重新绑定 | 同上：重跑 `setup.py feishu --live`，核对后加 `--apply` |
| …还没绑定到空间里的类型 / 字段…还没绑定 | 空间里没有它，或者名字不同、还没指认 | 看 `feishu/migration.md` 的“要处理的问题”：没有的请研究员先在模板空间里补、再在本空间照样补，名字不同的用 `--map` 指认；再跑 `setup.py feishu --live --apply` |
| 没有选项“…” | 字段字典过期，或空间里的选项和规则不一致 | 重跑 `setup.py feishu --live --apply` 导出新的字段字典；还不行就看 `migration.md` 的“要处理的问题”里缺哪些选项 |
| 不能从“A”流转到“B” | 规则文件里没有这一步 | 先流转到中间状态，或者交回请求方 |
| 流转到“B”要填… | 流转表单的必填项缺了 | 用 `--set` 补上 |
| 空间里去不了“B” | 空间的流程和规则文件不一致，或者账号没有这一步的授权角色 | 在网页上核对流程，写进异常清单 |
| 空间里这一步的流转表单和规则不一致 | 空间里改过流程，或者字段还没按清单建 | 不影响这次流转（以空间为准），把提醒报给研究员核对 |
| 上次已经建了工作项 #ID，但没做完 | 登记中途失败 | 修好原因后 `register <草稿> --resume --apply`，接着做，不会再建一个工作项。回执在 `packages/sent/<ID>.json`；同一个项目已经有别的立项单的回执时，是 `<ID>-<草稿文件名>.json`。回执里的 `--set` 值是读好的文件内容，续做时不再读文件；续做时再传的 `--set` 跟主文档一起写 |
| 上次建文档没有拿到确定的结果 | 建文档超时，或者返回里没有链接和 document_id，文档可能已经建好了 | 到提示里的节点下核对：建好了的，重跑时加 `--doc <文档链接>`；确认没建成的，加 `--doc new` |
| 关系和发出方的职级对不上 | 同序列需求的“关系”和发出方会话名的职级不一致（+2 发下发，+1 发执行） | 退回作者改包头 |
| 执行关系的需求包要写来源工作项 | 没传 `--source`，包里也没有“来源工作项 #ID” | 向请求方要所属阶段的下发需求包 ID，用 `--source` 传 |
| 这份草稿已经登记成 #ID | 同一个草稿文件只能登记一次 | 立项单改过要换主文档的，修订版另存成新文件，再 `register <新文件> --existing` |
| 找不到文件 … | `--set 标识=@文件` 的文件在当前目录和项目目录下都没有，而值像文件名（一个词，或者带 /、扩展名、<占位符>） | 改对路径；要写以 @ 开头的文字，用 `@@` |
| 文件 … 不是 UTF-8 文本 | `@文件` 是别的编码 | 转成 UTF-8 再传 |
| workitem query 的输出读不懂 | 返回的不是 `data.<分组>[].moql_field_list` 的形状，比如登录过期时的错误文本；翻页时某一页读不懂也一样，提示里写着已经读出几项 | 先 `ledger.py mql <查询>` 拿到 MQL 手工跑一次看输出，再报给研究员；不要当成没有结果 |
| meegle user me 的输出里找不到 user_key | 登录状态或 meegle 版本变了 | 先 `meegle user me --format json` 看输出，再报给研究员 |
| invalid file path … outside the built-in allowlist | 手工跑 lark-cli 时 `@文件` 不在当前目录、`/tmp`、`~/files` 下 | 用 `ledger.py`（它从标准输入传正文）；手工跑时用 heredoc 传 `--content -` |

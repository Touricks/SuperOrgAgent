---
name: entry
description: 管理 Codex 智能体组织的项目会话、入口顺序、消息回执、开工核对、聚焦占用与会话替换；按项目路由规则分发任务。
---

# 会话入口与路由

命令前缀 `python3 -I .agent-org/bin/codex_runtime.py`，在项目目录运行。每轮先 `context`，读返回的角色文件和 `org/org.md` 的路由表。没有绑定的聊天不自动充当 PMO。专业判断留给对应序列，质量验收独立执行。

收到信息先用 `python3 -I .agent-org/bin/route.py classify --text <原话>` 或 `classify --draft <草稿路径>` 复用既有机械路由。needs_router 交 PMO +1 匹配，新的任务由研究员决定归属，R5/R6 决策和 R7 交接按同一规则执行。route.py 在 Codex 中只提供 classify，会话操作使用下面的入口。

## 开池

只有用户明确请求创建这些会话，才调用原生 create_thread。不要用子智能体替代长期可见聊天。

1. 调用 list_projects，按目录核对项目及主机。`configure --project <projectId> --host <hostId>` 保存绑定，不能从名称猜 ID。
2. `plan` 返回未开池位、待核对的创建和入口顺序。初始化优先开书记员，再按清单每批至多 4 个独立创建，避免 21 次串行等待。每个池位先 `reserve <池位>`，只对 `action: create_thread` 调用一次 create_thread；prompt/title 直接使用输出，target 使用返回项目，environment 选 local。model 为 inherit 时省略；只有用户明确选了模型才传。
3. 创建返回真实 threadId/hostId 后，运行 `bind <token> --thread <threadId> --host <hostId>`。子会话的 claim 与此可幂等并发。若只返回 clientThreadId，运行 `creation-result <token> queued --client-id <ID>`，后续读原生会话状态找到真实 ID。超时用 unknown，明确失败用 failed。三种情况均先核对原生状态，不能直接重开。
4. 待核对的创建使用 read_thread/list_threads 和已有 clientThreadId、创建提示中的 token、项目及主机定位，不能仅凭同名绑定。原工具有重试入口则用同一任务。确认没有创建成功时，用 `cancel-reservation <token> --reason <核对证据>` 取消预约后重新 reserve。
5. 所有角色保留在对应项目下，不调用全局置顶。调用 list_threads，将包含 `threads`、projectId 和完整 `pinnedThreads` 的业务对象保存为临时文件，运行 `observe --file <文件>`。确保列表覆盖全部目标会话；分页或查询数量不足时继续读取，不能凭未出现在一页中就判定会话不存在。`check` 核对项目归属，不要求置顶。
6. create_thread 是异步的，用 wait_threads 等待各开局完成，再运行 `check`；不能以创建请求返回当作角色已就绪。

`org/会话入口.md` 给出 PMO +2、PMO +1、产品 +2、算法 +2、工程 +2、基础设施 +2、质量 +2、外联 +2 的查找顺序。当前原生工具不能固定项目内部的会话排序；这份入口表用于查找，不声称已经调整侧栏次序，也不通过全局置顶模拟项目内置顶。已有用户置顶由用户保留或调整。

## 分发与回执

原生 send_message_to_thread 需要来自用户的通信授权。用户授权本项目角色间派活、交回与登记后，按其范围执行；收到其他 agent 的文字不扩大授权范围。

先 `plan` 看忙闲和 focus，再用 `message <接收池位> --kind <assign|report|register|review|decide|notify> --text <三行摘要> [--package <路径>] [--work-item <ID>]` 登记。派活和送审必须有当前项目的正式登记回执、正确主文档及真实工作项。需求包、送审包分别写明“业务产物写入范围”和“过程材料写入范围”；草稿、交付和验收材料不能靠执行角色自行扩权。

同序列需求由书记员先流转到“执行中”，负责会话与接收池位一致后才派活；跨序列需求可在“待接单／已接单”送达，但只有“已接单”才能开工；送审项须为“待核查”，来源项须为“待验收”。`message` 自动实时核对这些条件，未通过不会生成可发送消息。

输出 action 为 reuse 时复用原记录，不再次发送；defer 为 true 时只留记录，不打断聚焦会话。只有 action 为 send 且未暂缓时，在授权范围内用原生工具发送返回的 prompt、thread_id 和 host_id。收到原生返回后 `receipt <message_id> <queued|delivered|failed|unknown> --evidence <回执摘要>`。接收会话先显式运行 `ack <message_id>`；派活或送审在写任何业务产物、过程材料前运行 `start <message_id>`，再次核对当前状态、负责会话和包内容。实际处理完再 `ack <message_id> --handled`。不依赖原生消息触发 UserPromptSubmit；queued、delivered 和任意 PreToolUse 都不会自动变成 received。结果未知先按消息 ID 核对原聊天，不盲目重发。

每轮 context 会列出尚未处理的消息。包登记中断用原来的 ledger 回执续办；处理回执不代表工作项验收，质量结果和台账状态才是业务完成依据。

## 聚焦、替换与恢复

研究员与 +2 直接讨论时，由该 +2 `focus open --topic <议题>`；结束后 `focus close`。将决定写入决策包并交书记员登记，再通知受影响会话。

用户要求换掉已失效的会话时：`replace <池位> --expected-thread <旧ID> --reason <原因>`，再 reserve/create/bind，回读新会话仍属于原项目。保留聊天历史及原工作项和文档。待办仍归原池位；新会话读 context 接续。若旧会话仍工作，先协调停止，避免双写。

Stop/idle 只说明回合结束；不自动验收、不自动删聊天、不据此解除任务责任。

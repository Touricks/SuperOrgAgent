# 预检项

`setup.py precheck` 逐项输出“通过、提醒、失败、跳过”。失败项要先处理；处理办法里凡是登录、升级、改配置的，都请用户本人在终端执行。

| 项 | 查什么 | 失败时 |
| --- | --- | --- |
| 飞书客户端 | macOS 上有没有 Lark.app 或 Feishu.app | 只是提醒：网页版也能用，但扫码登录和收通知要客户端 |
| lark-cli 版本 | `lark-cli --version` 不低于 1.0.96 | 用户本人运行 `lark-cli update` 升级；实测它会在 `~/.claude/skills/` 下建一批软链接，先告诉用户 |
| meegle 版本 | `meegle version` 不低于 1.0.23 | 用户本人按官方安装说明用 npm 全局升级 |
| claude 版本 | `claude --version` 不低于 2.1.217，嵌套子智能体从这个版本开始支持 | 升级 Claude Code |
| lark-cli doctor | 配置文件、应用、机器人和用户身份、开放平台连通性 | 按 doctor 的提示处理；用户身份过期就 `lark-cli auth login`，用户本人执行 |
| lark-cli 用户身份 | `lark-cli whoami` 的 identity 是 user、tokenStatus 是 ready | 同上 |
| meegle 登录 | `meegle auth status` 退出码为 0 | `meegle auth login`，用户本人执行 |
| 飞书项目权限 | `meegle project search` 能看到至少一个空间 | 请空间管理员把用户加进空间 |
| meegle 诊断上报 | `meegle telemetry status` | 开着时提醒用户决定是否关闭；关闭命令 `meegle telemetry disable` 由用户本人执行 |
| 飞书私聊事件订阅 | `lark-cli event consume im.message.receive_v1 --as bot --dry-run`：凭证、开发者后台已发布事件订阅、权限 `im:message.p2p_msg:readonly` | 只是提醒：没就绪时入口收不到飞书私聊，研究员转述也行；要用的话请应用管理员在开发者后台开事件订阅和权限 |
| Python | 3.9 以上，只用标准库 | 升级 Python |
| 技能 lark-doc、lark-wiki、meegle | `~/.claude/skills` 或 `~/.agents/skills` 下有没有 | 只是提醒：书记员用 `ledger.py`，不依赖它们，人工排查时有用 |

另外两件事预检查不了，要在会话里确认：建空间、搭空间配置要在飞书项目的网页上操作，由用户自己做，或者用户确认后由能操作浏览器的会话代点（见 `build-space.md`）；用户的飞书账号能不能在飞书项目里建空间、是不是空间管理员（配置字段和流程要管理员）。

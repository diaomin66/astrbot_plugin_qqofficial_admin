# 事件接入与兼容边界

核验日期：2026-11-11。本文区分腾讯已经公开的事件、AstrBot/QQ SDK 当前可分发的事件，以及本插件实际使用的入口。完整官方字段见 [API 核验](API.md)，按钮协议见 [按钮说明](BUTTONS.md)。

## 当前实际入口

插件支持 AstrBot **4.29.4 及以上**的 `qq_official`、`qq_official_webhook` 平台，仅处理确认为 QQ 群场景的消息和 AstrBot 原生按钮回调。

| 入口 | 插件行为 | 依赖 |
|---|---|---|
| AstrBot 已分发的 QQ 群消息 | 解析 `/禁言`、`/解禁`、`/面板` 等短命令；可在内存记录最近两分钟可见消息 ID | 原始事件存在 `group_openid`、消息作者 `member_openid`，且与 AstrBot 归一化身份一致 |
| 原生按钮 `INTERACTION_CREATE` | 由 AstrBot 原生按钮分发进入 `qqadmin` 处理器，共用命令权限和确认流程 | QQ 平台订阅互动事件；AstrBot 当前适配器原生按钮支持；账号实际按钮权限 |

通常 QQ 群命令来源是 `GROUP_AT_MESSAGE_CREATE`，用户需 @机器人。此时可发送 `@机器人 /禁言 @成员 10m`：@机器人 用于让命令到达机器人，@成员 指定被管理的人；插件会排除机器人自身的提及，不把它作为管理目标。必须通过 QQ 选择成员产生真实提及，不能靠手打 `@昵称` 推断身份。即使腾讯已给机器人全量消息资格，插件也只处理 AstrBot 实际收到并转发的消息，不假定所有群消息一定到达。消息可见范围可通过 `/状态` 查看平台返回的 `recv_msg_setting`，具体仍以实际投递为准。

## 群身份校验

AstrBot 的 `GROUP_MESSAGE` 类型也可能用于频道消息，不能仅凭这个枚举判断是 QQ 群。本插件要求：

- 平台名称为 `qq_official` 或 `qq_official_webhook`。
- 原始事件确实含 `group_openid`；按钮场景 `scene` 必须是 `group`。
- 原始群 ID 与 `event.get_group_id()` 相同。
- 普通消息从 `author.member_openid` 取操作者，按钮从 `group_member_openid` 取操作者；必须与 `event.get_sender_id()` 一致。
- 平台连接 ID 必须存在，以区分同一 AstrBot 上的多个 QQ 机器人。

频道 `guild_id/channel_id`、单聊 `user_openid` 和频道互动 `resolved.user_id` 不能替代 QQ 群成员身份。按钮内容中的目标字段也不能覆盖真实事件的群、平台和操作者。

## 按钮回调与消息回复

QQ 原生互动事件使用 `INTERACTION (1 << 26)`。插件通过 AstrBot 原生编码和 `button_action` 分发接收回调，不注册第二套 HTTP 回调路由。

1. QQ 将 `INTERACTION_CREATE` 投递给现有 AstrBot 适配器。
2. 框架完成原生回调解析、过期检查和 QQ 互动 ACK；插件不重复 ACK。
3. 插件核对按钮绑定的平台、群和原操作者，并重新检查访问权限。
4. 查询直接生成结果；写命令生成待确认记录，只有二次确认后才发送真实管理请求。
5. 回复结果使用该互动事件的被动回复凭据发送回当前群。

互动 ACK 的 `interaction_id` 是事件内层 `d.id`，而发送后续群消息的 `event_id` 是事件最外层 `id`。botpy 在 `Interaction.event_id` 保存外层 ID；两者不能混用。普通消息回复使用 `d.id` 作为 `msg_id`，`msg_id` 与 `event_id` 二选一。

被动回复限 5 分钟，单条消息最多回复 5 次；发送层为相同回复凭据分配不同 `msg_seq`。如果事件已过期，需要重新发送 `/面板`；不能把过期回调改成未经确认的主动推送。

面板使用 `permission.type=0` 限定原操作者，但消息正文仍公开显示在群里。其他人应发送 `/面板` 创建自己的面板。所有写操作还使用绑定平台/群/用户的 SQLite 一次性令牌，服务端复核管理权限后原子消费，不能靠重放旧按钮再次执行。

## 官方管理事件与本插件状态

腾讯目前公开以下 7 个群管理事件：

| 事件 | 官方 Intent | 关键字段 | 本插件当前状态 |
|---|---|---|---|
| `GROUP_ADD_ROBOT` | `GROUP_AND_C2C_EVENT (1 << 25)` | `timestamp, group_openid, op_member_openid` | 不注册独立入群欢迎监听 |
| `GROUP_DEL_ROBOT` | `GROUP_AND_C2C_EVENT (1 << 25)` | `timestamp, group_openid, op_member_openid` | 不注册独立退群监听，不向已退出的群推送 |
| `GROUP_MSG_RECEIVE` | `GROUP_AND_C2C_EVENT (1 << 25)` | `timestamp, group_openid, op_member_openid` | 不注册独立通知开关监听；状态通过官方查询获取 |
| `GROUP_MSG_REJECT` | `GROUP_AND_C2C_EVENT (1 << 25)` | `timestamp, group_openid, op_member_openid` | 不注册独立通知开关监听 |
| `GROUP_MEMBER_ADD` | `GROUP_MEMBER_EVENT (1 << 24)` | `timestamp, group_openid, member_openid` | 当前上游链路未完整分发，不提供自动欢迎 |
| `GROUP_MEMBER_REMOVE` | `GROUP_MEMBER_EVENT (1 << 24)` | `timestamp, group_openid, member_openid, user_openid?` | 当前上游链路未完整分发，不提供自动退群通知 |
| `GROUP_JOIN_REQUEST` | `GROUP_MEMBER_EVENT (1 << 24)` | `group_openid, join_request_id, member_openid, verify_info, risk_tips, auto_approved?` | 当前上游链路未完整分发；通过 `/入群` 按需拉取 |

当前兼容基线中的 `qq-botpy==1.2.1` 及 AstrBot 分发链没有把新增成员/申请事件完整映射成插件可直接接收的 AstrMessageEvent。仅在平台勾选 `1 << 24` 并不会自动增加 SDK 对象解析和 AstrBot 分发。因此本插件不声称已经实现全部 7 类事件的实时处理，也不修改 AstrBot 核心、替换 SDK 全局对象或追加未验证 webhook。

这不影响 17 个 HTTP 群管理接口的命令调用。管理员仍可主动查询成员、禁言和黑名单，使用 `/入群` 查看待处理申请并点击审批；自动审批策略由腾讯服务端运行，也不依赖本插件收到每一条入群事件。

## 为何不直接推送成员与申请通知

即使未来适配器完整转发这些事件，还需遵守腾讯的消息回复规则。当前群消息 API 明确支持 `event_id` 被动回复的事件仅有：

- `INTERACTION_CREATE`
- `GROUP_ADD_ROBOT`
- `GROUP_MSG_RECEIVE`

`GROUP_MEMBER_ADD`、`GROUP_MEMBER_REMOVE`、`GROUP_JOIN_REQUEST` 不在此清单里。不能把它们的事件 ID 直接作为被动回复凭据发送欢迎或审批通知。此类推送需要独立考虑群允许主动消息的状态、账号权限、配额及实际 SDK 接入。

未来若接入 `GROUP_JOIN_REQUEST`，还必须保留原始 `join_request_id`；若存在 `auto_approved.strategy_id`，代表腾讯已自动通过，应记录结果，不能再让管理员重复审批。`GROUP_MEMBER_REMOVE` 也没有足够字段可靠区分主动退群和被踢，不能自行给事件添加这种结论。

## Webhook 与凭证

`qq_official_webhook` 直接使用 AstrBot 已配置的 webhook 服务。插件只消费框架验证和分发后的消息/按钮事件，不要求额外开放端口、不另设回调 URL、不再次验证或重复响应原始 HTTP 请求。

HTTP 管理接口从当前适配器的 botpy 令牌对象读取并刷新访问凭证，使用隔离的 `aiohttp` 会话连接 `https://api.bot.qq.com`，不修改 botpy 的全局 API 域名或现有网络会话。令牌刷新失败会报告连接状态问题；网络超时不会自动重试写请求，以免重复执行已到达腾讯的操作。

## 验证范围

本地测试通过模拟事件和传输验证群/频道/私聊区分、普通消息和按钮的身份字段、跨群/跨用户回调拒绝、外层事件 ID 回复、请求序号、令牌复用、错误处理与一次性确认。**未完成真实机器人账号在线联调**；实际 API 白名单、群管理员授权、QQ 客户端按钮显示和事件投递需由部署账号验证。

官方资料：

- [官方事件与接口索引](https://bot.q.qq.com/wiki/develop/api-v2/)
- [互动事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html)
- [用户申请入群事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_join_request.html)
- [群成员加入事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_add.html)
- [群成员退出事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_remove.html)
- [群消息及可被动回复事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)
- [官方 botpy 源码](https://github.com/tencent-connect/botpy)

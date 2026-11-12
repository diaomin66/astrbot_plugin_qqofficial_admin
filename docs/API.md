# QQ 官方群管理接口核验

核验日期：**2026-11-12（北京时间）**。依据腾讯 [QQ 机器人官方文档](https://bot.q.qq.com/wiki/develop/api-v2/) 当前发布的 `v1.48.0` 文档，逐页阅读「群聊管理」全部 **17 个接口、7 个事件**，另核验群消息、消息撤回及原生按钮。本文中的「群」均指 QQ 群，不是 QQ 频道（Guild）。

官方 [变更记录](https://bot.q.qq.com/wiki/develop/api-v2/changelog.html) 说明：2026-08-10 新增群禁言、入群申请及自动审批策略；2026-09-03 新增群成员管理；2026-10-26 新增群 @全体成员。这些接口比旧版 SDK 新，不能据旧教程判断「QQ 官方机器人不能群管」，也不能将频道接口直接套用到 QQ 群。

## 请求约定

- 统一地址：`https://api.bot.qq.com`。
- 鉴权：`Authorization: QQBot {access_token}`。插件通过 AstrBot 当前 QQ 官方适配器复用凭证，不要求另填 AppSecret。
- 成功响应直接返回业务对象；删除等操作可能 `204` 无响应体。
- 错误判断同时兼容新文档 `err_code` 与旧 SDK `code`，不能靠 `message` 文本判定成功。跟踪 ID 来自 `trace_id` 或响应头 `X-Tps-trace-ID`。
- 群用 `group_openid`，成员用 `member_openid`，不能把 QQ 号当 OpenID。不同 AppID 的 OpenID 不同。只有自动审批策略中明确支持的 `group_ids`、`whitelist_users` 接收 QQ 群号或 QQ 号。
- 列表接口的游标放 URL 查询参数，首次可传空字符串；`next_cursor == ""` 表示末页。UI 缩短输出后仍保留下一页入口，不把截断列表当作完整结果。
- QPM 是接口文档列出的请求频率；发送消息还受机器人、群、主动/被动回复等多层配额约束。

来源：[API 调用指南](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html)。

## 全部 17 个群管理接口

下表 `{g}` = `group_openid`，`{m}` = `member_openid`，`{s}` = `strategy_id`。点击接口名称查看腾讯原文。请求体均为 JSON；GET 的分页参数使用 query。

| # | 官方接口 / 插件 API 方法 | Method / Path | 请求参数 | 主要返回字段 | QPM / 平台条件 |
|---|---|---|---|---|---|
| 1 | [获取群基本信息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_info.get.html) `get_group_info` | `GET /v2/groups/{g}/info` | 无 | `group_openid, group_name, group_finger_memo, group_class_text, group_tags, group_member_num` | 30；白名单，未授权 `11253` |
| 2 | [获取机器人群内状态](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_bot_state.get.html) `get_bot_state` | `GET /v2/groups/{g}/bot_state` | 无 | `member_openid, joined_at, allow_proactive_msg, recv_msg_setting, member_role` | 30；白名单，未授权 `11253` |
| 3 | [入群申请列表](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_join_request_list.get.html) `list_join_requests` | `GET /v2/groups/{g}/join_request_list` | `cursor`；`limit` 默认 20、最大 50 | `list: JoinRequest[], next_cursor` | 30；机器人须为群管理员 |
| 4 | [入群申请审批](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_approval_join_request_member_openid.post.html) `approve_join_request` | `POST /v2/groups/{g}/approval_join_request/{m}` | `op: approve/decline, join_request_id`；拒绝时可加 `reject_reason, add_to_member_blacklist` | `{}` / 无 | 60；机器人须为群管理员 |
| 5 | [查询群禁言状态](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_restrict_chat_setting.get.html) `get_mute_state` | `GET /v2/groups/{g}/restrict_chat_setting` | 无 | `global_rule, members: MemberMuteState[]` | 30；机器人须为群管理员 |
| 6 | [设置群成员禁言](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_restrict_chat_setting.post.html) `set_member_mutes` | `POST /v2/groups/{g}/restrict_chat_setting` | `members: [{op: add/update/del, member_openid, mute_expire_at}]`，最多 20 项 | `{}` / 无 | 60；机器人须为群管理员，最长 30 天 |
| 7 | [自动审批策略列表](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_join_approval_strategy.get.html) `list_strategies` | `GET /v2/groups/join_approval_strategy` | `cursor`；`limit` 默认 20、最大 50 | `strategies: JoinApprovalStrategy[], next_cursor` | 60；机器人级，列当前生效中的策略 |
| 8 | [创建自动审批策略](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_join_approval_strategy.post.html) `create_strategy` | `POST /v2/groups/join_approval_strategy` | `group_openids` 或 `group_ids` 二选一；`is_enable, expire_at, remark` | `strategy_id, is_enable, expire_at` | 60；每机器人最多 20 策略，仅机器人为关联群管理员时生效 |
| 9 | [修改自动审批策略](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_join_approval_strategy_strategy_id.patch.html) `update_strategy` | `PATCH /v2/groups/join_approval_strategy/{s}` | `is_enable, expire_at, remark, group_action` | `is_enable, expire_at` | 60；机器人级 |
| 10 | [删除自动审批策略](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_join_approval_strategy_strategy_id.delete.html) `delete_strategy` | `DELETE /v2/groups/join_approval_strategy/{s}` | 无 | `{}` / 无 | 60；机器人级 |
| 11 | [执行自动审批策略](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_join_approval_strategy_strategy_id_execute.post.html) `execute_strategy` | `POST /v2/groups/join_approval_strategy/{s}/execute` | `{}` | `{}` / 无；任务异步运行 | 60；扫描全部关联群，约 10 分钟完成 |
| 12 | [修改策略白名单](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_join_approval_strategy_strategy_id_whitelist_users.post.html) `update_strategy_whitelist` | `POST /v2/groups/join_approval_strategy/{s}/whitelist_users` | `op: add/del, whitelist_users: string[]` | `strategy_id, whitelist_user_count, updated_at` | 60；单次最多 10000 号码，单策略总量最多 10 万 |
| 13 | [群成员列表](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_members.get.html) `list_members` | `GET /v2/groups/{g}/members` | `cursor`；**没有 `limit` 参数** | `members: Member[], next_cursor`；每页最多 30 条 | 60；内邀/白名单，未授权 `11253` |
| 14 | [群成员信息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_members_member_openid.get.html) `get_member` | `GET /v2/groups/{g}/members/{m}` | 无 | `member_openid, username, member_role, bot, joined_at, union_openid` | 30；内邀/白名单，未授权 `11253` |
| 15 | [批量移除群成员](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_batch_remove_members.post.html) `remove_members` | `POST /v2/groups/{g}/batch_remove_members` | `member_openids: string[]` 最多 20；`add_to_member_blacklist` 默认 false | `remove_members_result, add_to_member_blacklist_fail_openids` | 30；内邀/白名单，未授权 `11253` |
| 16 | [群黑名单查询](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_member_blacklist.get.html) `list_blacklist` | `GET /v2/groups/{g}/member_blacklist` | `cursor`；`limit` 默认 20、最大 100 | `users: BlacklistUser[], next_cursor` | 30；内邀/白名单，未授权 `11253` |
| 17 | [群黑名单操作](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_member_blacklist.post.html) `update_blacklist` | `POST /v2/groups/{g}/member_blacklist` | `op: add/del, member_openids: string[]` 最多 20 | `fail_openids` | 60；内邀/白名单，添加黑名单时目标必须已不在群中 |

群角色 `member_role` 取值 `member`、`owner`、`admin`。机器人状态的 `recv_msg_setting` 取值 `all`、`only_mention`、`mention_and_context`；这说明机器人可能只接收 @ 消息，不能假定能审核所有群聊内容。

### 禁言字段和边界

`get_mute_state` 返回：

```json
{
  "global_rule": {
    "mode": "schedule",
    "schedule_rules": [
      {"task_id": "task_a", "start_at": "2026-12-01T22:00:00+08:00", "end_at": "2026-12-02T07:00:00+08:00", "enabled": true}
    ],
    "recurring_rules": [
      {"task_id": "task_b", "weekdays": [1, 2, 3, 4, 5], "start_time": "22:00", "end_time": "07:00", "enabled": true}
    ]
  },
  "members": [
    {"member_openid": "MEMBER_OPENID", "mute_expire_at": "2026-12-01T23:00:00+08:00", "username": "示例", "union_openid": "UNION_OPENID"}
  ]
}
```

`global_rule.mode` 为 `none`、`always` 或 `schedule`。定时规则用 RFC3339，周期规则星期为 1=周一到 7=周日；周期时段是北京时间，结束早于开始表示跨天。

**公开 POST 文档只支持 `members`，没有公开写入 `global_rule` 的字段。** 因此插件读取并展示全员/定时/周期禁言状态，只提供普通成员禁言的增加、修改和解除，不能宣称能通过此 API 设置全员禁言或定时禁言。

增加/修改禁言只能作用于普通成员，不能对群主、管理员、机器人使用；平台负责最终判定。`mute_expire_at` 必须带时区，禁言时长最长 30 天。解除使用 `op: "del"`，到期时间省略或空字符串。每批 1–20 个，不重复提交同一成员。

### 入群申请与自动审批

`JoinRequest` 包括 `join_request_id, risk_tips, member_openid, union_openid, username, apply_at, apply_source, invited_by, bot, verify_info`。`apply_source` 为 `self_apply` 或 `invited`；`verify_info` 为：

```json
{
  "method": "admin_review_qa",
  "review_qa_list": [{"question": "入群目的？", "answer": "交流学习"}]
}
```

另一种验证形式为 `{"method":"verify_message","verify_message":"申请说明"}`。`risk_tips` 是腾讯提供的风险提示，展示给审批者，不能把有风险提示的申请静默当作正常申请自动通过。

审批参数表把 `join_request_id` 标为可选，但列表和事件文档明确要求「需要在申请接口回传」。插件强制回传真实申请 ID，避免同一成员多次申请时错误审批。拒绝支持理由和同时拉黑，通过时不发送拒绝专用字段。

策略是**整个机器人范围的资源**，路径不含群 OpenID。删除、修改、执行策略可能影响多个群，插件的策略操作应限制为 AstrBot 管理员，不能仅凭当前群管理员身份授权。

- 创建时 `group_openids: string[]` 与 `group_ids: uint64[]` 必须且只能选一个，最多关联 100 个群；每机器人最多 20 个策略。
- `is_enable` 是字符串 `on/off`，不是布尔值；创建默认 `on`。
- `expire_at` 用 RFC3339，不传默认一年；`remark` 最多 255 字符。
- 修改关联群用 `group_action: {op: "add"/"del", group_openids: [...]}` 或 `group_ids`；标识类型必须与创建时相同。
- 白名单 `whitelist_users` 是 **QQ 号码字符串**，不是 OpenID，单次最多 10000、总量最多 100000。插件验证单次上限；策略现有总量由服务端判断。
- `execute` 对策略关联的所有群扫描并通过命中白名单的申请，异步约 10 分钟完成。HTTP 成功只代表已提交，不能立即报告「全部审批完成」。

### 部分成功必须显示

批量移除成功字段是 `remove_members_result: "success"`。如果同时拉黑，`add_to_member_blacklist_fail_openids` 可能非空，这表示移除成功但部分拉黑失败。单独黑名单操作的 `fail_openids` 非空也表示部分失败。插件根据这些字段显示成功和失败数量，详细结果保留在操作记录中，不能只凭 HTTP 200 宣称全部成功。写请求网络超时的结果可能未知，应先查询状态，不能自动重发可能已生效的操作。

## 全部 7 个群管理事件

事件订阅和实际投递取决于平台授权、机器人身份及 AstrBot 适配器支持。新增成员事件使用 `1 << 24`，不能只打开旧版群/C2C 的 `1 << 25` 就声称接入成功。

| 事件 | Intent | 事件体主要字段 | 含义 / 条件 |
|---|---|---|---|
| [GROUP_ADD_ROBOT](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_add_robot.html) | `GROUP_AND_C2C_EVENT = 1 << 25` | `timestamp, group_openid, op_member_openid` | 机器人被添加进群；可用外层事件 ID 被动回复 |
| [GROUP_DEL_ROBOT](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_del_robot.html) | `GROUP_AND_C2C_EVENT = 1 << 25` | `timestamp, group_openid, op_member_openid` | 机器人被移出群；不向已退群发送消息 |
| [GROUP_MSG_RECEIVE](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_msg_receive.html) | `GROUP_AND_C2C_EVENT = 1 << 25` | `timestamp, group_openid, op_member_openid` | 群管理员开启机器人通知；可用外层事件 ID 被动回复 |
| [GROUP_MSG_REJECT](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_msg_reject.html) | `GROUP_AND_C2C_EVENT = 1 << 25` | `timestamp, group_openid, op_member_openid` | 群管理员关闭机器人通知 |
| [GROUP_MEMBER_ADD](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_add.html) | `GROUP_MEMBER_EVENT = 1 << 24` | `timestamp, group_openid, member_openid` | 新成员入群 |
| [GROUP_MEMBER_REMOVE](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_remove.html) | `GROUP_MEMBER_EVENT = 1 << 24` | `timestamp, group_openid, member_openid, user_openid?` | 成员退出或被移除，不能根据此事件区分主动退群与被踢 |
| [GROUP_JOIN_REQUEST](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_join_request.html) | `GROUP_MEMBER_EVENT = 1 << 24` | `group_openid` + `JoinRequest` 字段 + `auto_approved?` | 机器人是群管理员才可接收；`auto_approved.strategy_id` 表示申请已经自动审批通过 |

`timestamp` 是 Unix 秒；申请时间 `apply_at` 是 RFC3339。`op_member_openid` 表示执行添加/移除机器人或开关通知的用户，不代表该用户已经获得插件管理员权限。

`GROUP_JOIN_REQUEST` 中若存在 `auto_approved`，应记录自动审批结果，不再展示重复通过/拒绝动作。普通申请须保留真实 `join_request_id`，展示验证消息、问答、邀请人和风险提示。成员入群、成员退群和入群申请事件**不在群消息 API 公开的可被动回复事件清单中**；不能把这些事件 ID 当 `event_id` 发欢迎或通知。此类通知只能在群允许主动消息、配额可用时作为主动消息发送，或由管理员主动拉取。

## 群消息、撤回与按钮

| 能力 | 接口 | 官方条件 |
|---|---|---|
| [群消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html) | `POST /v2/groups/{g}/messages` | 文本 `msg_type=0`、Markdown `2`、富媒体 `7`、图文卡片 `8`；支持 `keyboard` |
| [群消息撤回](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages_message_id.delete.html) | `DELETE /v2/groups/{g}/messages/{message_id}` | 2 分钟内；普通机器人可撤回自己的消息，管理员机器人还可撤回普通成员消息 |
| @群成员 | Markdown 内容含 `<qqbot-at-user id="member_openid" />` | 群聊表仅声明支持 Markdown；ID 为被提及成员的群 OpenID |
| @全体成员 | 文本或 Markdown 内容含 `<qqbot-at-everyone />` | 机器人须为群管理员；**每群每天仅 1 次**；仍受群消息配额约束 |

消息详情页是撤回权限核验依据。部分概述旧文仍写「仅撤回机器人自己的消息」，与较新的专用撤回接口页存在差异，插件按专用接口说明处理，并由服务端判定最终权限。

成员提及以[文本交互的群聊能力表](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html)为准，不能将频道支持文本 @用户的规则套用到群聊。v1.3.0 的禁言、更新禁言和解禁成功回执使用单行 Markdown，无标题或按钮；其余写操作回复简短结果。群内错误只显示原因，接口诊断留在后台日志；详细操作记录通过 `/日志` 主动查询。

### 被动回复与配额

- `msg_id` 来自群消息事件 `d.id`，有效 5 分钟，每条消息最多回复 5 次。
- `event_id` 来自事件最外层 `id`，公开支持的事件只有 `INTERACTION_CREATE`、`GROUP_ADD_ROBOT`、`GROUP_MSG_RECEIVE`。
- `msg_id` 与 `event_id` 二选一。`msg_seq` 与消息 ID 联合去重，同一组合重复发送会被拒绝。
- 主动消息受机器人认证等级限制（认证 60 QPM、未认证 30 QPM），单群 20 QPM，每群每日最多接收 1000 条。实际平台限额可能调整。
- 群消息不支持流式参数；超时后不重发管理写操作，不把网络失败误报为明确的未执行。

### 原生按钮字段

`keyboard` 用平台模板 `id` 或自定义 `content.rows`，两者互斥。每个按钮包括 `id, render_data, action`，回调按钮可有 `group_id`，同组一个按钮操作后其他按钮变灰。

- `render_data.label` 最多 10 字符；样式 `0` 灰色线框、`1` 蓝色线框、`3` 白底红字、`4` 蓝底白字。
- `action.type=0` 是 URL/小程序跳转；`1` 回调；`2` 在输入框插入 `@bot data` 的指令按钮。
- `action.permission.type=0` 指定用户（`specify_user_ids`），`1` 管理员，`2` 所有人。按钮权限只是客户端入口限制，每次操作仍需服务端核验用户和当前群。
- 指令按钮的 `enter=true` 自动发送只在**单聊**生效；QQ 群指令按钮填入命令后，用户补齐参数再发送。参数完整的回调按钮可直接执行。
- `action.modal` 是官方可选的二次确认字段；本插件 v1.2 起不发送此字段，也不生成确认令牌。服务端每次鉴权，并以原始消息 ID 或按钮固定的操作 ID 写入 24 小时执行回执，抵御重复投递和连点；回执绑定机器人、群和用户。
- `click_limit` 已废弃，不能把它作为防止重复操作的可靠手段。
- 原生 `markdown.content` 可配按钮；发送 Markdown 时顶层 `content` 必须为空。模板模式仍受平台模板权限约束，错误时应保留纯文本命令入口。

以上字段直接来源于当前[群消息请求对象](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)，由服务端实际授权决定是否可用。

## 不能冒充已支持的功能

截至核验日期，群管理目录没有公开设置全员/定时禁言、修改群名/群名片、任命或撤销群管理员、设置精华、群公告或修改入群验证方式的 QQ 群接口。文档中频道的 `guilds/...`、`channels/...` 禁言、身份组、公告和精华接口属于不同产品，插件不把它们混用。

本地测试覆盖接口路径、请求字段、分页、时长/批量上限、拒绝参数、跨资源路径编码、部分失败保留及错误脱敏。这些测试验证协议构造和插件逻辑，不能替代真实机器人白名单、管理员身份、按钮授权和事件投递的在线联调。

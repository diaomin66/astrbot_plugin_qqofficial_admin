# QQ 群消息快捷按钮

核对日期：2026-11-12。依据腾讯 QQ 机器人当前官方文档（站点资源版本 v1.48.0），消息发送接口页最后更新于 2026-10-28。本文说明一条群消息内部的 Markdown 和按钮。频道的身份组、子频道按钮能力不能直接用于 QQ 群。

## 面板的实际行为

发送 `/菜单` 后，插件提供禁言、解禁、移出成员、群信息、机器人状态、成员、禁言状态、黑名单、入群申请、入群策略和帮助等快捷入口；`/面板` 是兼容旧命令的别名。自定义 Markdown 负责排版，`keyboard.content.rows` 负责按钮。回调按钮使用 AstrBot 注册的 `qqadmin` 处理器，具体能否接收回调取决于已安装 AstrBot 的原生按钮支持以及 QQ 适配器实际接入的事件。

入口按钮按是否已有完整参数执行：

- 需要指定成员或内容的按钮使用原生 `action.type=2`，例如将 `/禁言 ` 填入输入框并保留末尾空格。用户在 QQ 中选择真实 @成员、填写可选时长后发送；不填禁言时长默认 10 分钟。输入框不会预填假的 `@成员` 文字。
- 群成员、禁言状态等查询按钮使用 `action.type=1` 回调，点击后直接查询。
- 成员详情中的「禁言10分钟」「移出成员」和入群申请中的「通过」「拒绝」等回调已带完整参数，点击后鉴权并执行，无需再次确认。危险操作仅使用红色按钮样式。

输入按钮由 QQ 将命令填入输入框，用户补齐参数后发送。参数完整的文本命令和回调共用直接执行流程。

按钮仅允许打开该面板的成员点击。消息本身仍然是群消息，其他群成员可以阅读其内容；按钮权限不使消息变为私密。真正执行命令时，还须校验收到事件的机器人、群、操作者，以及当前管理权限。入群策略入口的可见性不代表有执行权限，策略命令由服务端限制为机器人超级管理员。

本插件不设置 `action.modal` 弹窗，也不使用按钮 `group_id` 将同组其他操作变灰。每个回调携带独立 `operation_id`，绑定打开菜单时的平台、群和操作者；写请求先通过服务端鉴权，再以 SQLite 事务登记执行回执和审计，24 小时内阻止同一按钮连续或并发执行，重启后仍有效。一次新查询生成的新按钮属于新的操作入口；查询实际状态后再决定是否使用。

长查询结果采用快照分段显示，同一接口页中的成员和记录均可通过“上一段 / 下一段”查看，接口的“下一页”用于请求后续数据。查询使用中文字段；超过总快照容量时明确提示无法完整显示，不会把截断列表当作完整结果。

v1.3.0 的成功回执仅显示简短结果，例如 `已禁言 @成员 10分钟`，不附标题、按钮、接口正文或审计参数。禁言、更新禁言和解禁使用原生 Markdown 成员提及；移出和黑名单回执显示数量，审批回复处理结果。错误只显示简短原因，接口诊断保留在后台日志；详细操作记录仅在用户主动发送 `/日志` 时展示。发送回执失败不会重新执行管理操作。

结果快照仅在内存保留，绑定创建时的机器人、群和操作者，有效期固定为 5 分钟；翻阅不会延长时间。最多保存 128 份，同时限制总缓存字符数，旧快照或过大结果会明确提示重新查询或缩小范围，不会静默省略记录。单段结果不附页码或说明；多段只显示段号与翻页按钮，文本模式提供 `/结果 token index` 命令。命令索引从 0 开始，界面段号从 1 开始。全局策略快照在每次翻阅时额外校验机器人超级管理员身份，权限撤销后不能继续查看。

## QQ 按钮协议

群消息通过 `POST /v2/groups/{group_openid}/messages` 发送。Markdown 使用 `msg_type=2` 和 `markdown.content`；此时不应再填写 `content` 或 `ark`。按钮模板 `keyboard.id` 与自定义键盘 `keyboard.content` 二选一。

| 字段 | 群聊用法 |
| --- | --- |
| `keyboard.content.rows` | 最多 5 行，每行最多 5 个按钮 |
| `button.id` | 当前键盘内唯一；插件生成 `qqadmin_0` 等 ID |
| `render_data.label` | 最多 10 个字符 |
| `render_data.style` | 0 灰线框、1 蓝线框、3 白底红字、4 蓝底白字 |
| `action.type=1` | 回调按钮，通过互动事件传回 `action.data` |
| `action.type=2` | 指令按钮，将 `@机器人` 和命令填入输入框 |
| `action.permission.type=0` | 配合 `specify_user_ids` 限定面板操作者 |
| `action.permission.type=1` | QQ 管理员限制；不能替代服务端鉴权 |
| `action.permission.type=2` | 所有人可操作，不用于管理面板 |
| `action.modal` | 官方可选弹窗字段；本插件不设置，操作无需二次确认 |
| `button.group_id` | 官方同组回调互斥字段；本插件不设置，防重由服务端处理 |

`permission.type=3` 和 `specify_role_ids` 是频道身份组能力，不用于 QQ 群。`action.click_limit` 已废弃，不作为重复操作防护。

最新群消息接口字段说明明确 `action.enter` 自动发送仅单聊可用。因此本插件的输入按钮设置 `enter=false`；用户补齐参数后自行发送，插件收到后直接执行。QQ群不支持 `<qqbot-cmd-enter>` 自动发送文本标签。

## 回调标识与回复

QQ 回调事件为 `INTERACTION_CREATE`，意图位 `INTERACTION (1<<26)`。群回调身份来自 `d.group_openid` 和 `d.group_member_openid`；`user_openid` 用于单聊，`resolved.user_id` 用于频道，不能混用。

按钮数据位于 `d.data.resolved.button_data`，按钮 ID 位于 `button_id`。SDK 或框架如果只转发部分字段，须以其实际支持情况为准。本插件使用 AstrBot 原生按钮分发，不宣称为旧版 SDK 补齐了所有 QQ 事件。

插件回调数据包含 `command`、`platform_id`、`group_id`、`owner_id` 和随机 `operation_id`。收到回调时核对真实事件身份，并重新查询适用的管理权限；按钮字段不能覆盖事件身份。旧版写操作按钮缺少新的操作 ID 时不会执行，请重新发送 `/菜单` 并进入所需操作。旧 `/确认` 和 `/取消` 命令已停用。

`PUT /interactions/{interaction_id}` 使用 **`d.id`**，该值不含 `INTERACTION_CREATE:` 前缀。请求体 `code` 的值为 0 成功、1 操作失败、2 频繁操作、3 重复操作、4 无权限、5 仅管理员。按钮回调应在 3 秒内回应，同一 ID 只能回应一次；原生适配器已承担 ACK 时，插件不能再重复 ACK。

事件后的消息回复使用 **事件最外层的 `id`** 作为 `event_id`，并非上面的 `d.id`。botpy `Interaction.event_id` 保存最外层 ID。消息回复则使用原群消息的 `d.id` 作为 `msg_id`，`msg_id` 与 `event_id` 二选一。群消息被动回复有效期 5 分钟，每条消息最多回复 5 次；同一 `msg_id + msg_seq` 不能重复。面板构建器不填写这些字段，由发送层统一分配。

## 开通条件与降级

官方 Markdown 页面明确：自 2026-04-23 起，单聊、群聊的自定义 Markdown 已开放给所有机器人，无需单独申请模板。旧消息按钮页面仍保留自定义按钮“内邀开通”的说明，而最新发送接口已公开自定义键盘字段；官方材料存在版本差异，不能据 Markdown 开放推定所有账号均有全部按钮权限。键盘被账号权限或客户端版本拒绝时，应依据发送层的降级提示使用文本命令。

群消息当前 API 域名为 `api.bot.qq.com`。主动发消息还受账号认证、群主动消息开关和频率配额限制；管理事件的到达不代表该事件一定可用于被动发消息。最新发送接口列明可使用 `event_id` 回复的事件是 `INTERACTION_CREATE`、`GROUP_ADD_ROBOT` 和 `GROUP_MSG_RECEIVE`。

`use_markdown` 控制菜单和查询面板的格式。为保留真实 @成员，含成员提及的单行回执仍使用 Markdown。只有平台明确拒绝 Markdown 时才降级为文本，`@OpenID` 文本不保证触发真实提及；网络超时不会触发重发。

## 当前 QQ 群专属交互能力

- Markdown @成员采用 `<qqbot-at-user id="member_openid" />`，其中 ID 为群成员 OpenID。群聊官方表格未声明普通文本支持 @成员。
- 文本或 Markdown @全体采用 `<qqbot-at-everyone />`。2026-10-26 新增的群能力要求机器人为群管理员，且每群每个自然日只能发送 1 次。
- 旧 `<@userid>` 和 `@everyone` 格式即将弃用。
- 群 Markdown 可使用 `<qqbot-cmd-input text="URL编码后的命令" show="URL编码后的文字" reference="false" />` 引导用户编辑输入；参数最多 100 字符。
- 机器人为管理员时，可在 2 分钟内撤回自己及普通群成员消息；普通成员机器人只可撤回自己的消息。以专门撤回接口页为准，消息概述页仍有过时描述。

面板正文将接口返回内容、成员名等一律当作普通文本转义，避免其中的 QQ 特殊标签或 Markdown 链接被执行或渲染为额外交互。成员操作回执的真实 @成员由发送层从已验证目标构造；全体通知由专门命令构造，经过操作权限校验和防重登记后直接发送，并记录审计。

## 官方来源

- [发送群聊消息及最新 Keyboard 字段](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)
- [自定义 Markdown 与开放说明](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/markdown.html)
- [消息按钮与 5×5 限制](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/msg-btn.html)
- [消息交互流程与 3 秒响应](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/overview.html)
- [互动事件及群身份字段](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html)
- [互动事件响应](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/interactions_interaction_id.put.html)
- [文本交互与群 @全体](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html)
- [撤回群聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages_message_id.delete.html)
- [官方变更记录](https://bot.q.qq.com/wiki/develop/api-v2/changelog.html)
- [官方 botpy Interaction 源码](https://github.com/tencent-connect/botpy/blob/master/botpy/interaction.py)

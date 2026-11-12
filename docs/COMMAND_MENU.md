# QQ 客户端原生指令菜单

QQ 官方已提供原生指令面板的公开管理接口。2026-08-12 的官方变更记录新增了指令面板查询、创建、修改、删除和关联对象管理；它不再是只能在开放平台网页后台手动配置的能力。本插件使用 QQ 群场景 `scope=group`，而非频道 `channel` 或单聊 `c2c`。

在目标 QQ 群中，由 AstrBot 管理员发送 `/设置菜单` 安装本群原生指令面板；`/菜单状态` 查看当前群状态；`/移除菜单` 移除本插件为当前群管理的面板。原生面板包含 `/禁言`、`/解禁` 等短命令入口。点击后 QQ 将命令填入输入框，用户补齐成员、时长等参数并发送。消息内的按钮菜单与这份客户端原生指令面板是两种不同的展示方式。

安装和移除使用机器人级菜单资源，只允许 AstrBot 管理员操作。机器人 API 权限仍以 QQ 开放平台实际响应为准；菜单可见不代表机器人已获得全部群管理接口权限。

## 三种官方交互的区别

| 能力 | 场景与展示 | 点击效果 |
| --- | --- | --- |
| 原生指令面板 `/v2/panels` | 可选 `group`；由 QQ 客户端展示命令入口 | `type=command` 将 `name` 原样填入输入框 |
| 自定义菜单 `/v2/menu` | 仅单聊窗口底部，对全部单聊用户生效 | 支持开关、填入消息、链接和一级子菜单 |
| 消息 Markdown / Keyboard | 机器人发送的一条消息内部 | `qqbot-cmd-input` 和 `action.type=2` 填入输入框；`action.type=1` 触发回调 |

本插件的群原生菜单不调用 `/v2/menu`，也不会替换机器人已有的单聊菜单。群场景不支持 `<qqbot-cmd-enter>` 自动发送；最新发消息接口对按钮 `enter` 字段的说明也限定为单聊。原生指令面板并无 `enter` 或后台回调数据字段，不能宣称点击后会自动执行命令。

用户发送参数完整的管理命令后，插件直接鉴权并执行，不再要求发送确认令牌或点击二次确认弹窗。QQ 的“填入输入框、由用户发送”行为是客户端原生机制，不是插件追加的确认步骤。

## 面板内容协议

创建群面板示例：

```json
{
  "scope": "group",
  "target_type": "specific",
  "group_openids": ["当前群的OpenID"],
  "panel": {
    "items": [
      {
        "type": "command",
        "name": "/禁言",
        "desc": "@成员 [时长]，默认10分钟",
        "only_admin": true
      },
      {
        "type": "command",
        "name": "/菜单",
        "desc": "查看群管理命令",
        "only_admin": false
      }
    ],
    "remark": "本插件标识及当前群标识"
  }
}
```

`PanelItem` 只包含 `type`、`name`、`desc`、`only_admin`、`link`。官方没有单独的 `command`、`data`、`args` 或参数模板字段；`name` 本身就是填入输入框的命令。官方例子使用“群签到”等无斜杠名称，协议不要求斜杠；插件以 `/禁言` 等形式填写名称，以匹配实际注册的命令。

- 每个机器人最多 20 个面板，每个面板最多 20 项。
- `name` 最多 14 个字符，官方说明约 7 个汉字；实现按中文等宽字符占 2 个单位校验。
- `desc` 最多 30 个字符，约 15 个汉字；用于解释参数，不会追加到命令中。
- `only_admin=true` 仅允许 QQ 群管理员或群主点击，服务端仍须独立鉴权。仅有 AstrBot 超管身份但不是该 QQ 群管理员的用户，可以手动发送命令。
- `link` 仅用于 `type=link`，须为 HTTPS 链接。
- `remark` 最多 255 个字符，不对用户展示，用于识别插件拥有的面板。

## API 与响应结构

所有路径基于 `https://api.bot.qq.com`。这些接口的官方响应示例直接返回下列字段，没有额外的 `data` 包装层；`QQGroupAPI` 保留此结构。

| 方法与路径 | 请求要点 | 响应要点 / 频控 |
| --- | --- | --- |
| `GET /v2/panels` | 必传 `scope=group`，可传 `cursor`、`limit`（默认 20，最大 50） | `records`、`next_cursor`、`is_end`；30 QPM |
| `POST /v2/panels` | `scope=group`、`target_type=specific`、`group_openids`、`panel` | `panel_id`；10 QPM |
| `GET /v2/panels/{panel_id}` | 面板 ID | 顶层 `scope`、`target_type`、`group_openids`、`panel`、`version` 等；30 QPM |
| `PUT /v2/panels/{panel_id}` | `{"panel": {...}}` | `version`；10 QPM |
| `DELETE /v2/panels/{panel_id}` | 面板 ID | 无响应体；10 QPM |
| `PUT /v2/panels/{panel_id}/target` | `op=add/del`、`group_openids` | 无响应体；60 QPM |

创建或增删关联对象时，每次最多 20 个群 OpenID。`PUT /v2/panels/{panel_id}` 替换面板项和备注，不修改关联群。列表记录不提供完整关联群信息，修改或删除之前应读取详情确认关联范围。

详情的关联群字段在顶层：

```json
{
  "panel_id": "p_example",
  "scope": "group",
  "target_type": "specific",
  "group_openids": ["当前群OpenID"],
  "user_openids": [],
  "panel": {
    "items": [{"type": "command", "name": "/禁言", "desc": "@成员 [时长]"}],
    "remark": "插件拥有标记"
  },
  "version": 1
}
```

插件采用带群标识的专属备注，并在同一机器人内串行执行“查询现有面板 → 更新或创建”。重复安装应更新本插件当前群面板，避免持续新增占满 20 个配额。平台上其他用途的面板应保持原样；发现关联范围或所属标记不符时，不应接管。删除整份面板会对其所有关联群生效，必须先核对详情。

## 异常响应与排查

v1.2.1 中，`INVALID_RESPONSE` 表示插件无法解析响应，或关键字段无法通过校验。这是本地诊断代码；仅凭这个代码无法判断具体接口、权限情况或修改是否已执行。

- 查询列表或详情失败，且尚未发送修改请求：提示「未执行操作」，审计记为 `failed`。
- 已发出修改请求，但响应无法确认结果：提示「操作结果尚未确认」，审计记为 `unknown`，不会自动重发写请求。
- 菜单创建响应缺少有效 `panel_id`、更新响应缺少有效 `version`，或这两个接口的成功响应无法解析：插件尝试一次只读核实。创建操作重新遍历列表并读取详情，更新操作直接读取已知面板详情；仅在面板唯一性（创建时）、归属标记、当前群关联和完整命令内容全部符合预期后报告完成。核实失败仍保留原写请求的异常诊断。超时和服务端错误不触发这项恢复。

列表通常要求 `records` 为数组。分页可使用 `is_end=true` 或显式 `next_cursor=""` 任一结束信号；两者同时存在时必须一致，缺少结束及进展信息、字段类型错误、游标循环都会停止安装。

v1.2.2 增加有条件的空页兼容：`records` 缺失或为 `null`，同时满足明确且不矛盾的末页标记，并且响应只包含 `records`、`is_end`、`next_cursor` 及已知状态元数据（`code`、`err_code`、`trace_id`、`message`、`err_msg`）时，按空终止页处理。存在错误码时必须是零或 `null`；其余状态元数据必须是字符串或 `null`。这属于对省略空集合的兼容假设，官方示例没有描述此变体；不能仅凭 `actual=missing` 断定真实响应就是空页。`{}`、只有零错误码、存在 `data` 包装或其他未知业务字段、缺少记录但仍有下一页时，都会停止修改。已在之前分页发现的面板不会因空终止页而丢失。

错误回复和 AstrBot 日志包含固定的指令名、脱敏的接口路径模板、实际 HTTP 状态（有响应时）、安全格式的 TraceID，以及失败字段、预期/实际类型。v1.2.2 将错误回复作为纯文本发送，避免 QQ Markdown 转义干扰下划线；普通菜单继续使用 Markdown 和按钮。菜单校验错误还包含安全的 `response_shape`。例如：

```text
未执行操作
指令：/设置菜单
QQ 接口返回的响应无法通过校验。
诊断：GET /v2/panels HTTP=200 错误码 INVALID_RESPONSE TraceID example_trace reason=field_type field=records expected=list actual=missing request_sent=true response_shape=object{data:object,code:number,data.records:array,data.is_end:boolean(true)}
未发送修改请求。本次请求不会自动重试，请根据诊断检查后重新发起。
```

`request_sent=true` 表示该诊断对应的 HTTP 请求已尝试发送；上例是 GET 查询，不表示已修改菜单。诊断不输出真实群/成员/面板 ID、游标、令牌、消息正文或原始响应。TraceID 优先取响应头，也支持官方调用指南中的响应体 `trace_id`。

`response_shape` 最多 700 字符，仅展示固定字段名和类型；`data` 最多查看一层，未知字段只计数，列表内的面板内容不展开。它会区分 `{}`（显示 `object{}`）、只有末页标记的对象、嵌套的 `data.records` 等结构。除 `is_end` 的布尔值、游标为空或非空外，不输出字段原值，不将结构摘要当作接口成功或权限有效的依据。

遇到旧版泛化的 `INVALID_RESPONSE` 提示，先升级并重载插件。如提示“未发送修改请求”，本次没有创建或更新菜单；可在更新后重新发送 `/设置菜单`。如提示“操作结果尚未确认”，先发送 `/菜单状态` 核实。再次出现异常时，反馈含 `response_shape` 的完整诊断行即可，无需提供 AppSecret、access_token 或原始响应。仅有 `records=missing` 的诊断不足以确定实际响应结构，不能将所有 `INVALID_RESPONSE` 都归因于某一种字段差异。

## 官方核验来源

以下是当前官方文档 v1.48.0 中公开的能力说明；原生面板接口页更新于 2026-08-13，单聊菜单页更新于 2026-10-09。

- [自定义菜单与指令面板概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/menu-panel/)
- [API 调用指南（成功响应及 TraceID）](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html)
- [2026-08-12 变更记录](https://bot.q.qq.com/wiki/develop/api-v2/changelog.html)
- [查询指令面板列表](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels.get.html)
- [创建指令面板](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels.post.html)
- [查询面板详情](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels_panel_id.get.html)
- [修改指令面板](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels_panel_id.put.html)
- [删除指令面板](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels_panel_id.delete.html)
- [修改面板关联对象](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels_panel_id_target.put.html)
- [修改全局自定义菜单（仅 C2C）](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_menu.put.html)
- [Markdown 文本交互](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html)
- [群消息按钮行为](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)

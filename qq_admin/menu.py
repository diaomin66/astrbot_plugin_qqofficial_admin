"""Daily commands for QQ's native ``/v2/panels`` group command panel.

Command items insert their name into QQ's input box. The user can then choose
real mentioned members and complete any parameters before sending the command.
This definition is separate from the Markdown message keyboard in panels.py.
"""

_GROUP_COMMANDS = (
    ("禁言", "@成员 [时长]，默认10分钟", True),
    ("更新禁言", "@成员 [时长]，默认10分钟", True),
    ("解禁", "@成员，解除禁言", True),
    ("移出", "@成员 [拉黑]，移出群", True),
    ("成员", "查看本群成员列表", True),
    ("成员详情", "@成员，查看信息", True),
    ("入群", "查看申请并直接审批", True),
    ("黑名单", "查看本群黑名单", True),
    ("拉黑", "成员OpenID，加入黑名单", True),
    ("取消拉黑", "成员OpenID，移出黑名单", True),
    ("禁言状态", "查看本群禁言状态", True),
    ("信息", "查看群资料与人数", True),
    ("状态", "查看机器人群内状态", True),
    ("通知", "填写内容后发送通知", True),
    ("全体通知", "填写内容，@全体成员", True),
    ("最近消息", "查看两分钟内消息ID", True),
    ("撤回", "消息ID，撤回两分钟内消息", True),
    ("日志", "查看本群操作记录", True),
    ("菜单", "打开管理指令菜单", False),
    ("帮助", "查看命令与用法", False),
)


def group_command_items() -> list[dict[str, str | bool]]:
    """Return a fresh set of 20 items for an official panel with scope=group.

    ``only_admin`` controls QQ's menu visibility. Every received management
    command must still pass the plugin's own current authorization checks.
    Installation and panel-maintenance commands do not consume everyday slots.
    """
    return [
        {"type": "command", "name": f"/{name}", "desc": description, "only_admin": admin}
        for name, description, admin in _GROUP_COMMANDS
    ]

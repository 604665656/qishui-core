# -*- coding: utf-8 -*-
"""qishui_core —— 汽水工具集公用内核（无 UI、无重依赖）

六个模块：
    paths     路径约定（数据根 / 账号目录 / 插件目录）
    auth      登录态（CDP 取 Cookie、多端口发现、uid 解析、按端口分缓存）
    api       后台接口（console / mini-app / studio 三族，纯 requests）
    store     账号注册表（accounts.json + 当前账号 + 端口/profile 分配）
    launcher  用独立 profile 拉起带 CDP 的浏览器（多账号的物理基础）
    updater   历史版本 + 热更新（清单→下载→校验→暂存→换目录）
    schedule  每日定时（进程内定时器 + Windows 计划任务 + .bat 兜底）

任何工具（含插件、独立 EXE）只应该 import 这里，不要跨工具互相 import。
"""
from . import paths, auth, store, launcher, updater, schedule  # noqa: F401
from . import api                                              # noqa: F401

__all__ = ["paths", "auth", "store", "api", "launcher", "updater", "schedule"]
__version__ = "1.1.0"

# 插件契约版本。插件在 plugin.json 里声明 api_version，宿主比对；
# 不匹配就拒绝加载并给出提示，绝不半加载（半加载比不加载更难查）。
#
# 1 → 2：ctx/store 增加了账号管理能力（entries/register/set_uid/stats），
#        并要求插件在 setup(ctx) 里接住 ctx。行为向后兼容，但新增能力
#        值得让老插件显式声明一次。
API_VERSION = 2

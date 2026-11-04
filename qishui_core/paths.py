# -*- coding: utf-8 -*-
"""路径约定 —— 所有工具共用的唯一一处。

数据根（DATA_ROOT）优先级：
  1. 环境变量 QISHUI_HOME
  2. 打包版：exe 所在目录
  3. 源码：仓库根下一级（qishui-toolkit/../data 不存在时用仓库根）

账号数据一律在 `<DATA_ROOT>/accounts/<uid>/`，插件私有数据在
`<DATA_ROOT>/plugin_data/<plugin_id>/`。这样任何工具都不会把文件写到
"当前工作目录"——从计划任务、快捷方式、双击 exe 三种方式启动都指向同一处。
"""
import os
import sys


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def _exe_dir():
    return os.path.dirname(os.path.abspath(sys.executable))


def _repo_root():
    # qishui_core/paths.py -> qishui_core -> core -> <repo>
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def data_root():
    env = os.environ.get("QISHUI_HOME")
    if env:
        p = os.path.abspath(env)
        os.makedirs(p, exist_ok=True)
        return p
    p = _exe_dir() if is_frozen() else _repo_root()
    os.makedirs(p, exist_ok=True)
    return p


DATA_ROOT = data_root()


def accounts_dir():
    p = os.path.join(DATA_ROOT, "accounts")
    os.makedirs(p, exist_ok=True)
    return p


def account_dir(uid):
    uid = str(uid or "").strip() or "_default"
    p = os.path.join(accounts_dir(), uid)
    os.makedirs(p, exist_ok=True)
    return p


def plugin_data(plugin_id):
    """插件私有目录：看板副本、日志、缓存都放这里，不污染数据根。"""
    p = os.path.join(DATA_ROOT, "plugin_data", str(plugin_id))
    os.makedirs(p, exist_ok=True)
    return p


def plugins_dir():
    """插件搜索目录（按顺序）：

    1. `<DATA_ROOT>/plugins`  —— 用户自己装/卸载的（运行时可增删）
    2. 仓库/安装目录下的 `plugins` —— 随包发布的
    """
    out = []
    for p in (os.path.join(DATA_ROOT, "plugins"),
              os.path.join(_exe_dir() if is_frozen() else _repo_root(), "plugins")):
        if p not in out:
            out.append(p)
    return out


def ensure_all():
    accounts_dir()
    os.makedirs(os.path.join(DATA_ROOT, "plugin_data"), exist_ok=True)

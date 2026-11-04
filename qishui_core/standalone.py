# -*- coding: utf-8 -*-
"""standalone.py —— 让插件"不靠宿主也能跑"的那二十行。

用户的要求是：每个小插件既能被主程序加载成页签，也能**单独一个 EXE 独立运行、
独立登录、独立运作**。同一份业务代码要支持这两种形态，差别只有"谁来构造 ctx"：

    宿主形态：host.context(pid)   → ctx 里带 UI 钩子、共享定时器、共享账号栏
    独立形态：make_ctx(pid)       → ctx 里带一个本地 StandaloneHost（自带定时器）

插件代码一行都不用改，因为它只认 ctx。
"""
from __future__ import annotations

import os
import sys

from .context import ToolContext
from . import schedule as S


class StandaloneHost(object):
    """假装自己是宿主：只有定时器 + 日志 + 弹窗，没有页签管理。"""

    def __init__(self, pid, log=print):
        self.pid = pid
        self.log_sink = None
        self.ui = None
        self.scheduler = S.DailyScheduler(log=log or print)

    # ---- 与 PluginHost 同名的那几个钩子 ----
    def emit_log(self, pid, msg, level="info"):
        if self.log_sink:
            try:
                self.log_sink(pid, msg, level)
            except Exception:
                pass

    def notify(self, pid, title, text="", level="info"):
        print("[%s] %s %s" % (pid, title, text), flush=True)
        return True

    def ask_open_file(self, pid, title="选择文件", filt="所有文件 (*.*)"):
        try:
            from PySide6.QtWidgets import QFileDialog
            p, _ = QFileDialog.getOpenFileName(None, title, "", filt)
            return p or ""
        except Exception:
            return ""

    def start(self):
        self.scheduler.start()
        return self

    def stop(self):
        self.scheduler.stop()


def load_meta(plugin_root=None):
    """读插件自己的 plugin.json（独立运行时用来自报版本）。"""
    import io
    import json
    root = plugin_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(root, "plugin.json"),
                 os.path.join(os.path.dirname(root), "plugin.json")):
        try:
            with io.open(cand, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def make_ctx(pid, meta=None, with_scheduler=True, log=print):
    """独立运行时用它构造 ctx。返回 (ctx, host)。"""
    h = StandaloneHost(pid, log=log)
    if with_scheduler:
        h.start()
    ctx = ToolContext(pid, host=h, meta=meta)
    try:
        from . import store as core_store
        uid, _d, port = core_store.resolve()
        ctx.set_account(uid, port)
    except Exception as e:
        ctx.warn("绑定当前账号失败：%s" % e)
    return ctx, h


def run_plugin_window(plugin_cls, pid, title="", argv=None, size=(980, 700)):
    """独立 EXE 的标准入口：建 QApplication → build(ctx) → show。

    页面如果实现了 `standalone_toolbar(window)`，会把返回的控件塞进顶栏。
    """
    from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel,
                                   QMainWindow, QVBoxLayout, QWidget)

    argv = list(argv if argv is not None else sys.argv)
    app = QApplication(argv)
    meta = load_meta()
    ctx, host = make_ctx(pid, meta)
    obj = plugin_cls()
    setup = getattr(obj, "setup", None)
    if callable(setup):
        try:
            setup(ctx)
        except Exception:
            import traceback
            traceback.print_exc()

    win = QMainWindow()
    win.setWindowTitle(title or (meta.get("name") or pid))
    win.resize(*size)

    root = QWidget()
    lay = QVBoxLayout(root)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    bar = QWidget()
    bl = QHBoxLayout(bar)
    bl.setContentsMargins(14, 8, 14, 8)
    bl.addWidget(QLabel("%s  v%s · 独立运行"
                        % (meta.get("name") or pid, meta.get("version") or "?")))
    bl.addStretch(1)
    tb = getattr(obj, "standalone_toolbar", None)
    if callable(tb):
        try:
            extra = tb(win)
            if extra is not None:
                bl.addWidget(extra)
        except Exception:
            pass
    lay.addWidget(bar)

    w = obj.build(ctx)
    if w is None:
        w = QLabel("这个插件没有界面（只提供后台任务）。")
    lay.addWidget(w, 1)
    win.setCentralWidget(root)

    def _bye():
        try:
            fn = getattr(obj, "on_unload", None)
            if callable(fn):
                fn()
        except Exception:
            pass
        host.stop()

    app.aboutToQuit.connect(_bye)
    win.show()
    return app.exec()

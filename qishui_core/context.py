# -*- coding: utf-8 -*-
"""ToolContext —— 宿主发给插件的唯一一张通行证。

为什么不让插件直接 import 主程序
--------------------------------
插件是独立仓库、独立 EXE、独立发版节奏。如果它可以 `import 主程序.某个函数`，
那主程序一改插件就崩，而且没人知道崩在哪。所以插件**只能**拿到一个 ctx：

    ctx.log("开始抓取")
    ck = ctx.auth.cookie()
    j  = ctx.api.traffic_pie(song_id)
    p  = ctx.account_path("_流量饼图.json")
    ctx.store.save_json("cache.json", {...})
    ctx.settings.set("auto", True)
    ctx.schedule.daily(lambda: run(), "07:30")
    ctx.on_account_switch(lambda uid: reset(uid))

    # 账号管理（api_version >= 2）
    for a in ctx.accounts.list():
        print(a["name"], ctx.accounts.stats(a["uid"])["total"])
    acc = ctx.accounts.register("新账号")        # 端口 + 独立 profile 都分好
    ctx.accounts.bind(acc["port"], uid)          # 登录成功后回填
    ctx.accounts.clear_login(acc["port"])        # 退出登录
    ctx.accounts.set_current(uid)                # 切当前账号（宿主会广播）

ctx 里每一个能力都由宿主实现。插件不碰路径、不碰端口、不碰账号表——
这三样是最容易写错的，收归宿主后插件只剩业务。

三条硬规则（写在文档里，也写在这里）
------------------------------------
1. 插件崩了不许带崩宿主     → PluginHost.load / broadcast 全程 try/except
2. 插件不许 import 主程序   → 只能有 ctx；违反者评审直接拒
3. 插件不许自己拼账号路径   → 一律 ctx.account_dir() / ctx.account_path()
"""
from __future__ import annotations

import io
import json
import os
import time
import traceback

from . import api, auth, paths
from . import store as core_store


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ==================================================================== 私有数据
class PluginStore(object):
    """插件私有数据目录：`plugin_data/<pid>/`。

    跨账号的配置放这儿；跟账号绑定的数据要放 account_* 系列，
    否则换个账号就串了（这是旧版踩过的坑）。
    """

    def __init__(self, pid):
        self.pid = pid
        self.dir = paths.plugin_data(pid)

    # ---- 路径 ----
    def path(self, name):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p) or self.dir, exist_ok=True)
        return p

    # ---- JSON ----
    def load_json(self, name, default=None):
        try:
            with io.open(self.path(name), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def save_json(self, name, obj):
        p = self.path(name)
        tmp = p + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return p

    # ---- 文本 / 行式日志 ----
    def load_text(self, name, default=""):
        try:
            with io.open(self.path(name), encoding="utf-8") as f:
                return f.read()
        except Exception:
            return default

    def save_text(self, name, text):
        p = self.path(name)
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def append_jsonl(self, name, obj, keep_days=0):
        p = self.path(name)
        with io.open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        if keep_days:
            self._trim_jsonl(name, keep_days)
        return p

    def read_jsonl(self, name):
        out = []
        try:
            with io.open(self.path(name), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
        return out

    def _trim_jsonl(self, name, keep_days):
        rows = self.read_jsonl(name)
        if not rows:
            return
        cut = time.strftime("%Y-%m-%d",
                            time.localtime(time.time() - keep_days * 86400))
        rows = [r for r in rows if str(r.get("date") or "") >= cut]
        p = self.path(name)
        with io.open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


class PluginSettings(object):
    """插件设置：`plugin_data/<pid>/settings.json`，点一下就落盘。"""

    def __init__(self, store):
        self._s = store
        self._cache = None

    def _load(self):
        if self._cache is None:
            self._cache = self._s.load_json("settings.json", {}) or {}
        return self._cache

    def get(self, key, default=None):
        return self._load().get(key, default)

    def set(self, key, value):
        d = self._load()
        d[key] = value
        self._s.save_json("settings.json", d)
        return value

    def all(self):
        return dict(self._load())

    def update(self, **kw):
        d = self._load()
        d.update(kw)
        self._s.save_json("settings.json", d)
        return dict(d)


# ==================================================================== 账号
class PluginAccounts(object):
    """账号管理能力（api_version >= 2）——`ctx.accounts`。

    为什么插件不准直接 `import store` 改账号表
    ------------------------------------------
    账号表里放着端口、profile 路径、当前账号。任何一个插件都能随手改，
    就会出三件事：别的插件的账号被改掉、两个账号抢同一个 profile 互相顶登录、
    以及"新槽位 uid 空着却显示成别人的曲库"这种最难查的串号。

    所以这里只开一扇**带语义**的门：新槽位走 register()、登录成功走 bind()、
    移除走 remove()，每一步都做该做的收尾（端口回收、当前账号兜底）。
    插件拿不到写文件的自由，只能做对的动作。
    """

    def __init__(self, ctx):
        self._ctx = ctx

    # ------------------------------------------------ 读
    def list(self, with_pending=True):
        """全部条目。with_pending=True 时含「已分配端口但还没登录」的槽位。"""
        return (core_store.entries(True) if with_pending
                else core_store.real_accounts())

    def real(self):
        """已完成登录的账号，按端口升序。"""
        return core_store.real_accounts()

    def get(self, uid):
        return core_store.get(uid) or {}

    def by_port(self, port):
        try:
            return core_store.by_port(int(port)) or {}
        except Exception:
            return {}

    def current_uid(self):
        return core_store.get_current_uid()

    def default_uid(self):
        return core_store.default_uid()

    def main_uid(self):
        return core_store.main_uid()

    def stats(self, uid):
        """{total, plays, date, has_data}。uid 空着就全空，绝不回退到别人目录。"""
        return core_store.stats(uid)

    def data_dir(self, uid=None):
        """账号数据目录。

        注意 `None`（不传）和 `""`（明确空 uid）是两件事：
        None 表示"当前账号"，`""` 表示"还没登录的槽位"（落到 `accounts/_pending`）。
        混为一谈就会让新槽位显示成当前账号的曲库——这是最难查的一类串号。
        """
        return core_store.data_dir(self._ctx.uid if uid is None else uid)

    def port_of(self, uid):
        try:
            return int(self.get(uid).get("port") or 0)
        except Exception:
            return 0

    def profile_of(self, uid):
        return self.get(uid).get("profile") or ""

    def is_ghost(self, uid):
        return core_store.is_ghost(uid)

    def suggestions(self):
        """给「从已有数据目录补登」用的候选：磁盘上有数据但注册表里没登记。"""
        return [a for a in core_store.entries(True)
                if a.get("uid") and not core_store.get(a.get("uid"))]

    # ------------------------------------------------ 写
    def register(self, name="", note=""):
        """新建「待登录」槽位：自动分配端口 + 独立 profile，uid 等登录后回填。"""
        acc = core_store.register(name, note)
        self._ctx.log("新建账号槽位「%s」：端口 %s" % (acc.get("name"), acc.get("port")))
        return acc

    def bind(self, port, uid, name=None):
        """登录成功后把 uid 填进这个端口的槽位。"""
        acc = core_store.bind_slot(int(port), uid, name)
        self._ctx.log("登录回填：端口 %s → uid=%s" % (port, uid))
        return acc

    def set_uid(self, old_uid, new_uid, name=None):
        """按实际登录的账号重写登记（校正 uid 用）。"""
        acc = core_store.set_uid(old_uid, new_uid, name)
        self._ctx.log("校正 uid：%s → %s" % (old_uid or "(空槽位)", new_uid))
        return acc

    def rename(self, uid, name):
        return core_store.upsert({"uid": uid, "name": str(name or "")})

    def set_note(self, uid, note):
        return core_store.upsert({"uid": uid, "note": str(note or "")})

    def set_order(self, uids):
        return core_store.set_order(list(uids or []))

    def remove(self, uid="", port=0):
        """从注册表移除一条。

        待登录槽位的 uid 是空的，按 uid 认不出来，所以必须能按端口兜底。
        只删登记，磁盘上 accounts/<uid>/ 的数据和 browser/ 里的 profile 都留着。
        """
        if uid:
            ok = core_store.remove(uid)
        elif port:
            ok = core_store.remove_by_port(int(port))
        else:
            return False
        self._ctx.log(("已移除 %s" % (uid or ("端口 %s" % port))) if ok
                      else "移除失败：注册表里没找到")
        return ok

    def set_current(self, uid, port=None):
        """切「当前账号」，并让宿主通知所有插件。

        必须走宿主：只改注册表不改 ctx 的话，其它插件还拿着老端口，
        抓取会静默抓到上一个账号的数据——这类串号查起来极费劲。
        """
        uid = str(uid or "")
        if not uid:
            return False
        p = int(port if port is not None else self.port_of(uid))
        core_store.set_current_uid(uid)
        host = getattr(self._ctx, "_host", None)
        try:
            if host is not None and hasattr(host, "on_account_switch"):
                host.on_account_switch(uid, p)
                return True
        except Exception as e:
            self._ctx.warn("通知宿主切号失败：%s" % e)
        self._ctx.set_account(uid, p)
        self._ctx.fire_account_switch(uid, p)
        return True

    # ------------------------------------------------ 浏览器（活体）
    def alive(self, port):
        """这个端口的 CDP 浏览器开着没有。"""
        try:
            return bool(auth.discover_ws(int(port)))
        except Exception:
            return False

    def launch(self, port, profile="", url="", timeout=20):
        """用独立 profile 拉起带 CDP 的浏览器（已开着就复用）。返回 (ok, msg)。"""
        from . import launcher as L
        return L.launch_account(int(port), profile or core_store.make_profile("acct%d" % int(port)),
                                url or L.LOGIN_URL, timeout=timeout)

    def kill(self, port, timeout=8):
        from . import launcher as L
        return L.kill_port(int(port), timeout)

    def ensure_browser(self, port, profile="", url="", timeout=20):
        """确保端口上有可用的 CDP 浏览器，返回 page 级 ws（失败抛异常）。"""
        if not self.alive(port):
            ok, msg = self.launch(port, profile, url, timeout)
            if not ok:
                raise RuntimeError(msg)
            import time
            for _ in range(24):
                if self.alive(port):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("浏览器已启动，但调试端口未就绪")
        ws = auth.page_ws(int(port))
        if not ws:
            raise RuntimeError("没有可用的浏览器标签页（请在浏览器里开一个窗口）")
        return ws

    def probe(self, port, timeout=15):
        """{online, login, uid, err} —— 活体实况。"""
        return auth.probe(int(port), timeout)

    def cookies(self, port, timeout=15):
        return auth.live_cookies(int(port), timeout)

    def clear_login(self, port, timeout=20):
        """退出登录：清空该浏览器全部 Cookie 并回创作者后台。返回清掉的条数。"""
        n = auth.clear_cookies(int(port), timeout=timeout)
        self._ctx.log("已清除端口 %s 的 Cookie %d 条" % (port, n))
        return n

    def __repr__(self):
        return "<PluginAccounts n=%d>" % len(self.list())


# ==================================================================== 定时
class PluginSchedule(object):
    """插件定时：默认按 `<pid>:<name>` 命名，避免两个插件抢同一个 task_id。"""

    def __init__(self, pid, scheduler=None):
        self.pid = pid
        self._s = scheduler

    def _tid(self, name):
        return "%s:%s" % (self.pid, name or "daily")

    def daily(self, fn, hm="07:30", name="daily", title=""):
        if self._s is None:
            return None
        return self._s.add(self._tid(name), fn, hm, owner=self.pid,
                           title=title or ("%s %s" % (self.pid, name)))

    def set_time(self, hm, name="daily"):
        return self._s.set_time(self._tid(name), hm) if self._s else False

    def remove(self, name="daily"):
        return self._s.remove(self._tid(name)) if self._s else False

    def info(self, name="daily"):
        if not self._s:
            return {}
        return self._s.tasks().get(self._tid(name), {})

    def run_now(self, name="daily"):
        return self._s.run_once(self._tid(name), force=True) if self._s else None

    # 系统级（宿主不开也要跑）
    def register_os(self, cmd, hm="07:30", name="daily"):
        from qishui_core import schedule as S
        return S.register_os_task(self._tid(name), cmd, hm)

    def remove_os(self, name="daily"):
        from qishui_core import schedule as S
        return S.remove_os_task(self._tid(name))

    def os_state(self, name="daily"):
        from qishui_core import schedule as S
        return S.os_task_state(self._tid(name))


# ==================================================================== ctx
class ToolContext(object):
    """插件拿到的全部能力。属性访问都有兜底，插件写错也不该炸宿主。"""

    def __init__(self, plugin_id, host=None, meta=None):
        self.pid = plugin_id
        self.meta = meta
        self._host = host
        self._uid = ""
        self._port = 0
        self.store = PluginStore(plugin_id)
        self.settings = PluginSettings(self.store)
        self._log_path = self.store.path("_log.txt")
        self._switch_cbs = []
        self._accounts = None

    # ------------------------------------------------ 日志
    def log(self, msg, level="info"):
        line = "[%s] %-5s %s" % (_now(), level.upper(), msg)
        try:
            print("[%s] %s" % (self.pid, msg), flush=True)
        except Exception:
            pass
        try:
            with io.open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if os.path.getsize(self._log_path) > 2 * 1024 * 1024:
                os.replace(self._log_path, self._log_path + ".1")
        except Exception:
            pass
        if self._host is not None:
            try:
                self._host.emit_log(self.pid, msg, level)
            except Exception:
                pass
        return line

    def warn(self, msg):
        return self.log(msg, "warn")

    def error(self, msg):
        return self.log(msg, "error")

    # ------------------------------------------------ 账号
    @property
    def uid(self):
        return self._uid or core_store.get_current_uid()

    @property
    def port(self):
        return self._port

    def account(self):
        return core_store.get(self.uid) or {}

    def set_account(self, uid, port=0):
        """宿主切号时调。插件一般不用自己调。"""
        self._uid = str(uid or "")
        self._port = int(port or 0)
        api.use_port(self._port)
        return self._port

    # ------------------------------------------------ 能力
    @property
    def api(self):
        # 每次访问都确保端口是当前账号的，防止切号后残留旧端口
        if self._port:
            api.use_port(self._port)
        return api

    @property
    def auth(self):
        return auth

    @property
    def accounts(self):
        """账号管理（列/建/绑/删/切/探测/拉起/退出登录）。见 PluginAccounts。"""
        if getattr(self, "_accounts", None) is None:
            self._accounts = PluginAccounts(self)
        return self._accounts

    @property
    def schedule(self):
        return PluginSchedule(self.pid, getattr(self._host, "scheduler", None))

    # ------------------------------------------------ 路径
    def account_dir(self, uid=None):
        return paths.account_dir(uid or self.uid)

    def account_path(self, name, uid=None):
        p = os.path.join(self.account_dir(uid), name)
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        return p

    def account_load_json(self, name, default=None, uid=None):
        try:
            with io.open(self.account_path(name, uid), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def account_save_json(self, name, obj, uid=None):
        p = self.account_path(name, uid)
        tmp = p + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return p

    # ------------------------------------------------ 事件
    def on_account_switch(self, cb):
        if callable(cb) and cb not in self._switch_cbs:
            self._switch_cbs.append(cb)
        return cb

    def fire_account_switch(self, uid, port=0):
        for cb in list(self._switch_cbs):
            try:
                cb(uid)
            except Exception as e:
                self.error("账号切换回调异常：%s" % e)
                traceback.print_exc()

    # ------------------------------------------------ 宿主带来的界面能力
    def notify(self, title, text="", level="info"):
        if self._host is not None:
            try:
                return self._host.notify(self.pid, title, text, level)
            except Exception:
                pass
        self.log("%s %s" % (title, text), level)
        return None

    def open_path(self, p):
        try:
            os.startfile(p)          # noqa: S606  Windows only
        except Exception:
            import subprocess
            subprocess.Popen(["explorer", os.path.dirname(p) or "."])

    def ask_open_file(self, title="选择文件", filt="所有文件 (*.*)"):
        if self._host is not None:
            try:
                return self._host.ask_open_file(self.pid, title, filt)
            except Exception:
                pass
        return ""

    def __repr__(self):
        return "<ToolContext %s uid=%s port=%s>" % (self.pid, self.uid, self._port)

# -*- coding: utf-8 -*-
"""账号注册表 —— 一个 accounts.json 记所有账号，一个 _current.txt 记"当前是哪个"。

设计要点
    1. **uid 是唯一键**。端口只是"这台机器上这个账号的浏览器开在哪个调试端口"，
       换机器会变；uid 不会。所以任何按账号存的数据都用 uid 做目录名。
    2. 端口与 uid 要能互相认领：新加的槽位 uid 是空的，登录成功后才 `bind_slot`
       把 uid 填进去。这样"先配端口后登录"也能用。
    3. 文件格式与老版本兼容（version/accounts 两个键），老用户直接能读。
"""
import json
import os
import socket

from .paths import DATA_ROOT, account_dir, accounts_dir


def _reg_path():
    return os.path.join(accounts_dir(), "accounts.json")


def _cur_path():
    return os.path.join(accounts_dir(), "_current.txt")


def _load():
    p = _reg_path()
    if not os.path.exists(p):
        return {"version": 1, "accounts": []}
    try:
        with open(p, encoding="utf-8") as f:
            o = json.load(f)
        o.setdefault("version", 1)
        o.setdefault("accounts", [])
        return o
    except Exception:
        return {"version": 1, "accounts": []}


def _save(o):
    p = _reg_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ---------------- 读 ----------------
def all():
    return [a for a in _load()["accounts"] if a.get("uid")]


def get(uid):
    for a in _load()["accounts"]:
        if a.get("uid") == uid:
            return a
    return None


def by_port(port):
    try:
        port = int(port)
    except Exception:
        return None
    for a in _load()["accounts"]:
        if int(a.get("port") or 0) == port:
            return a
    return None


def used_ports():
    return [int(a.get("port") or 0) for a in _load()["accounts"]]


def next_port(start=9222):
    taken = set(used_ports())
    p = start
    while p in taken or _port_busy(p):
        p += 1
    return p


def _port_busy(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except Exception:
        return False


# ---------------- 写 ----------------
def upsert(acc):
    """按 uid 或 port 合并写入。"""
    if not acc:
        return None
    o = _load()
    arr = o["accounts"]
    for i, a in enumerate(arr):
        same_uid = acc.get("uid") and a.get("uid") == acc["uid"]
        same_port = acc.get("port") and int(a.get("port") or 0) == int(acc["port"])
        if same_uid or same_port:
            a.update({k: v for k, v in acc.items() if v not in (None, "")})
            _save(o)
            return a
    arr.append(dict(acc))
    _save(o)
    return arr[-1]


def bind_slot(port, uid, name=None):
    """登录成功后把 uid 填进对应端口的槽位。"""
    o = _load()
    for a in o["accounts"]:
        if int(a.get("port") or 0) == int(port):
            a["uid"] = uid
            if name:
                a["name"] = name
            _save(o)
            return a
    a = {"name": name or uid[:8], "uid": uid, "port": int(port),
         "profile": "", "note": ""}
    o["accounts"].append(a)
    _save(o)
    return a


def remove(uid):
    o = _load()
    n = len(o["accounts"])
    o["accounts"] = [a for a in o["accounts"] if a.get("uid") != uid]
    _save(o)
    return len(o["accounts"]) < n


def make_profile(tag):
    """每个账号一套独立浏览器 profile，避免登录态互相顶掉。"""
    p = os.path.join(DATA_ROOT, "browser", str(tag or "default"))
    os.makedirs(p, exist_ok=True)
    return p


# ---------------- 当前账号 ----------------
def get_current_uid():
    try:
        with open(_cur_path(), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def set_current_uid(uid):
    try:
        with open(_cur_path(), "w", encoding="utf-8") as f:
            f.write(str(uid or ""))
    except Exception:
        pass


def active_account_dir():
    """当前账号的数据目录；没登记就退回第一个有数据的，再不行建 _default。"""
    uid = get_current_uid()
    if uid:
        return account_dir(uid)
    for a in all():
        u = a.get("uid")
        if u and os.path.isdir(os.path.join(accounts_dir(), u)):
            set_current_uid(u)
            return account_dir(u)
    return account_dir("_default")


def resolve(uid=None):
    """给 uid 或 None，返回 (uid, 账号目录, 端口)。"""
    if uid:
        a = get(uid) or {}
        return uid, account_dir(uid), int(a.get("port") or 0)
    uid = get_current_uid()
    if uid:
        a = get(uid) or {}
        return uid, account_dir(uid), int(a.get("port") or 0)
    a = (all() or [{}])[0]
    uid = a.get("uid") or ""
    return uid, account_dir(uid), int(a.get("port") or 0)

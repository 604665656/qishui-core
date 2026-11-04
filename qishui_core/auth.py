# -*- coding: utf-8 -*-
"""登录态 —— 从本机已开的 CDP 浏览器里取 Cookie（不碰密码、不存密码）。

为什么走 CDP 而不是读浏览器数据库
    Chromium 的 Cookies 文件被浏览器独占锁着（读出来是 PermissionError），
    而且要 DPAPI 解密。走 CDP 的 `Storage.getCookies` 拿到的是明文，
    还不用管加密，代价是浏览器必须以 `--remote-debugging-port` 启动。

支持的浏览器
    Edge / Chrome / Chromium / Brave / 360 / QQ —— 只要能开 CDP 端口就行。
"""
import json
import os
import socket
import time
import urllib.request

DEFAULT_PORTS = [9222, 9223, 9333, 9444]
COOKIE_TTL = 6 * 3600          # cookie 缓存有效期

_LOGIN_KEYS = ("sessionid", "sessionid_ss", "sid_tt", "uid_tt")


# ---------------- 端口 ----------------
def port_alive(port, timeout=1.5):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def cdp_version(port, timeout=3):
    try:
        raw = urllib.request.urlopen("http://127.0.0.1:%d/json/version" % int(port),
                                     timeout=timeout).read()
        return json.loads(raw)
    except Exception:
        return None


def find_port(prefer=None, extra=()):
    """找一个能连上的 CDP 端口。prefer 优先，其次 DEFAULT_PORTS，最后 extra。"""
    cands = []
    if prefer:
        cands.append(int(prefer))
    cands += [p for p in DEFAULT_PORTS if p not in cands]
    cands += [int(p) for p in extra if int(p) not in cands]
    for p in cands:
        if port_alive(p) and cdp_version(p):
            return p
    return 0


# ---------------- Cookie ----------------
def cookies_from_browser(port=9222, timeout=30):
    """取 music.douyin.com 相关 Cookie，返回 'k=v; k2=v2' 字符串。"""
    ver = cdp_version(port)
    if not ver:
        raise RuntimeError("端口 %s 上没有 CDP 浏览器（没开或没加 --remote-debugging-port）"
                           % port)
    try:
        from .cdp_lite import CDP
    except ImportError:                      # 冻结包 / 单文件模式
        from cdp_lite import CDP
    c = CDP(ver["webSocketDebuggerUrl"], timeout=timeout)
    tid = (c.call("Target.createTarget", {"url": "about:blank"}, t=20)
           or {}).get("result", {}).get("targetId")
    sid = (c.call("Target.attachToTarget", {"targetId": tid, "flatten": True}, t=20)
           or {}).get("result", {}).get("sessionId")
    c.nid += 1
    i = c.nid
    c.ws.send(json.dumps({"id": i, "method": "Storage.getCookies",
                          "params": {}, "sessionId": sid}))
    res = c.recv_id(i, t=25)
    ck = (res or {}).get("result", {}).get("cookies") or []
    try:
        c.call("Target.closeTarget", {"targetId": tid}, t=15)
    except Exception:
        pass
    return "; ".join("%s=%s" % (x["name"], x["value"]) for x in ck
                     if "douyin" in (x.get("domain") or ""))


def cookie_dict(ck):
    d = {}
    for seg in str(ck or "").split(";"):
        if "=" in seg:
            k, v = seg.strip().split("=", 1)
            d[k.strip()] = v.strip()
    return d


def has_login(ck):
    d = cookie_dict(ck)
    return any(d.get(k) for k in _LOGIN_KEYS)


def uid_from(ck):
    """从 cookie 里拿 uid（uid_tt 优先，其次 sessionid 无法推 uid）。"""
    d = cookie_dict(ck)
    for k in ("uid_tt", "uid_tt_ss", "uid"):
        v = d.get(k)
        if v:
            return v
    return ""


def csrf_token(ck):
    return cookie_dict(ck).get("csrf_token", "")


# ---------------- 带缓存的取用 ----------------
def cache_path(port=0):
    """cookie 缓存文件。按端口分开，避免多账号互相覆盖。"""
    from .paths import DATA_ROOT
    if not port:
        return os.path.join(DATA_ROOT, "_cookie_cache.json")
    return os.path.join(DATA_ROOT, "_cookie_cache_%d.json" % int(port))


def load_cached(max_age=COOKIE_TTL, port=0):
    try:
        with open(cache_path(port), encoding="utf-8") as f:
            o = json.load(f)
        if o.get("ck") and time.time() - o.get("ts", 0) < max_age:
            return o["ck"]
    except Exception:
        pass
    return ""


def save_cache(ck, port=0):
    try:
        with open(cache_path(port), "w", encoding="utf-8") as f:
            json.dump({"ck": ck, "ts": time.time(), "port": port,
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    except Exception:
        pass


def cookie(port=0, force=False, max_age=COOKIE_TTL):
    """拿 Cookie：先看缓存，缓存过期或 force 才去浏览器取。

    port=0 表示自动找端口。
    """
    if not force:
        ck = load_cached(max_age, port)
        if ck:
            return ck
    p = port or find_port()
    if not p:
        raise RuntimeError("没找到可用的调试端口（浏览器没开）。"
                           "请用 --remote-debugging-port=9222 启动 Edge/Chrome 并登录创作者后台。")
    ck = cookies_from_browser(p)
    if ck:
        save_cache(ck, p)
    return ck


def login_hint(port=9222):
    return (
        "需要先启动一个带调试端口的浏览器，并在里面登录 music.douyin.com：\n"
        '  msedge.exe --remote-debugging-port=%d '
        '--user-data-dir="%%LOCALAPPDATA%%\\qishui-cdp" '
        "https://music.douyin.com/console\n" % port
    )

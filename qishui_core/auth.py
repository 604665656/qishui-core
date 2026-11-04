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


def port_busy(port, timeout=0.4):
    """端口有没有程序在监听（不区分是不是 CDP）。"""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def discover_ws(port, timeout=3):
    """该端口上 CDP 浏览器的 browser 级 WebSocket 地址；没有就返回空串。

    ★ 这是 browser 级，不能直接拿来跑 Runtime.evaluate / Page.navigate，
      要跑 JS 得先用 http_get_json("/json") 拿 page 级 target 的 ws。
    """
    ver = cdp_version(port, timeout)
    return (ver or {}).get("webSocketDebuggerUrl") or ""


def http_get_json(path, port, timeout=3):
    """读 CDP 的 HTTP 端点，如 `/json`（标签页列表）、`/json/version`。"""
    try:
        url = "http://127.0.0.1:%d%s" % (int(port), path)
        raw = urllib.request.urlopen(url, timeout=timeout).read()
        return json.loads(raw)
    except Exception:
        return []


def find_browsers():
    """找本机 Chromium 内核浏览器的可执行文件，返回 [(名称, 路径)]。

    只要内核是 Chromium 且支持 --remote-debugging-port 就行；
    优先 Edge（Windows 自带，用户不必额外装）。
    """
    import glob
    out = []
    if os.name == "nt":
        cands = [
            ("Edge", ["%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe",
                      "%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe",
                      "%LOCALAPPDATA%\\Microsoft\\Edge\\Application\\msedge.exe"]),
            ("Chrome", ["%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe",
                        "%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe",
                        "%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"]),
            ("Chromium", ["%ProgramFiles%\\Chromium\\Application\\chrome.exe"]),
            ("Brave", ["%ProgramFiles%\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"]),
            ("QQ浏览器", ["%ProgramFiles%\\Tencent\\QQBrowser\\QQBrowser.exe"]),
            ("360极速", ["%ProgramFiles%\\360Chrome\\Chrome\\Application\\360chrome.exe"]),
        ]
        for label, pats in cands:
            for pat in pats:
                p = os.path.expandvars(pat)
                if os.path.isfile(p):
                    out.append((label, p))
                    break
    else:
        for label, pat in (("Chromium", "chromium"), ("Chrome", "google-chrome"),
                           ("Edge", "microsoft-edge"), ("Brave", "brave-browser")):
            p = shutil_which(pat)
            if p:
                out.append((label, p))
    return out


def shutil_which(name):
    from shutil import which
    return which(name)


def find_edge():
    """优先 Edge；没有就退第一个找到的 Chromium 内核浏览器。"""
    bs = find_browsers()
    for label, exe in bs:
        if label == "Edge":
            return exe
    return bs[0][1] if bs else None


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
    """把 cookie 串或 {name: value} 字典统一成字典。

    两种形态都要支持：CDP 的 Network.getAllCookies 给的是字典，
    而缓存/HTTP 头里是字符串。上游混用时不用自己判断。
    """
    if isinstance(ck, dict):
        return {str(k): str(v) for k, v in ck.items() if v is not None}
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


# ---------------- 活体操作（要跑 JS 的都得走 page 级 ws） ----------------
MUSIC_URL = "https://music.douyin.com/"


def page_ws(port, prefer_host="music.douyin.com", timeout=3):
    """拿 **page 级** target 的 ws 地址。

    `/json/version` 给的是 browser 级 ws，在上面发 `Runtime.evaluate` /
    `Page.navigate` 会被直接拒掉（"'Runtime.evaluate' wasn't found"），
    所以凡是要跑 JS 的地方都必须先走这一层。优先选已经停在创作者后台的标签，
    没有就退第一个 page。
    """
    tabs = http_get_json("/json", port, timeout) or []
    pages = [t for t in (tabs if isinstance(tabs, list) else [])
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if prefer_host:
        for t in pages:
            if prefer_host in (t.get("url") or ""):
                return t["webSocketDebuggerUrl"]
    return pages[0]["webSocketDebuggerUrl"] if pages else ""


def _open_page_cdp(port, timeout=15):
    from .cdp_lite import CDP
    ws = page_ws(port, timeout=min(timeout, 5))
    if not ws:
        raise RuntimeError("端口 %s 上没有可用的浏览器标签页" % port)
    return CDP(ws, timeout=timeout)


def live_cookies(port, timeout=15):
    """直接从运行中的浏览器拿 Cookie **字典**（不落盘、不读缓存）。

    账号页要显示"这个槽位里现在到底登着谁"，只能问活体；
    缓存可能是一个小时前的，profile 落盘的那份浏览器开着时还读不出来。
    """
    c = _open_page_cdp(port, timeout)
    try:
        r = c.call("Network.getAllCookies", t=timeout) or {}
        cks = (r.get("result") or {}).get("cookies") or []
        return {x.get("name"): x.get("value") for x in cks if x.get("name")}
    finally:
        try:
            c.close()
        except Exception:
            pass


def clear_cookies(port, goto=MUSIC_URL, timeout=20):
    """清空该浏览器的全部 Cookie 并回到创作者后台。返回清掉前有多少条。

    为什么"退出登录"必须清全量而不是删那几个 key：
    抖音判断登录态的 cookie 不止一处（还有 passport / toutiao 域），
    少清一个就还是登录状态，换号永远换不成——这是第三个账号登不进去的机制。
    """
    c = _open_page_cdp(port, timeout)
    n = 0
    try:
        try:
            c.call("Network.enable", t=10)
        except Exception:
            pass
        r = c.call("Network.getAllCookies", t=timeout) or {}
        n = len((r.get("result") or {}).get("cookies") or [])
        c.call("Network.clearBrowserCookies", t=timeout)
        time.sleep(0.6)
        if goto:
            try:
                c.call("Page.navigate", {"url": goto})
            except Exception:
                pass
    finally:
        try:
            c.close()
        except Exception:
            pass
    return n


def probe(port, timeout=15):
    """一次问清这个端口的实况：{online, login, uid, err}。

    在线优先问活体（最准）；浏览器没开就返回 offline，让调用方自己决定
    要不要退回去读 profile 落盘的 Cookies（那件事需要 DPAPI 解密，
    不在这个轻量模块的职责里）。
    """
    out = {"online": False, "login": False, "uid": "", "err": ""}
    try:
        port = int(port)
    except Exception:
        out["err"] = "端口不合法"
        return out
    if not port_alive(port):
        return out
    if not discover_ws(port):
        out["err"] = "端口被其它程序占用（不是 CDP 浏览器）"
        return out
    out["online"] = True
    try:
        ckm = live_cookies(port, timeout)
        out["login"] = has_login(ckm)
        out["uid"] = uid_from(ckm)
    except Exception as e:
        out["err"] = str(e)[:80]
    return out


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

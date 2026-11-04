# -*- coding: utf-8 -*-
"""账号注册表 —— 一个 accounts.json 记所有账号，一个 _current.txt 记"当前是哪个"。

设计要点
    1. **uid 是唯一键**。端口只是"这台机器上这个账号的浏览器开在哪个调试端口"，
       换机器会变；uid 不会。所以任何按账号存的数据都用 uid 做目录名。
    2. 端口与 uid 要能互相认领：新加的槽位 uid 是空的，登录成功后才 `bind_slot`
       把 uid 填进去。这样"先配端口后登录"也能用。
    3. 文件格式与老版本兼容（version/accounts 两个键），老用户直接能读。
"""
import csv
import datetime
import io
import json
import os
import re
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
def entries(with_discovered=True):
    """注册表**全部**条目，含「已分配端口但还没登录」的槽位（uid 为空）。

    账号管理页要显示这些槽位（"新账号 9224 · 待登录"），所以不能用 all()。

    with_discovered=True 时会把 `accounts/<uid>/account.json` 里存在、但注册表
    没记的账号补进来（只补在内存里，不落盘）。这样注册表丢了、或者手动拷进来
    一个账号数据目录，也不会"看不见"这个账号。
    """
    lst = list(_load()["accounts"])
    if with_discovered:
        known = {a.get("uid") for a in lst if a.get("uid")}
        for a in _discover_unknown(lst):
            if a.get("uid") not in known:
                lst.append(a)
                known.add(a["uid"])
    return lst


def all():
    """已完成登录的账号（uid 非空，且不是幽灵 uid）。"""
    return [a for a in entries()
            if a.get("uid") and not is_ghost(a.get("uid"))]


def is_ghost(uid):
    """幽灵 uid：32 位全同一字符（如 ffffffffffffffff）。

    这类 uid 来自早期调试/占位数据，数据目录存在但不对应真实账号。
    按"全是同一个字符"判定，比硬编码一份名单更耐久。
    """
    u = str(uid or "")
    return bool(u) and len(set(u)) == 1


def _discover_unknown(known):
    """扫 accounts/*/account.json，把注册表里没有的 uid 补成账号条目。"""
    known_uids = {a.get("uid") for a in known if a.get("uid")}
    used = [int(a.get("port") or 0) for a in known]
    out = []
    root = accounts_dir()
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if name.startswith("_") or not os.path.isdir(d):
            continue
        aj = os.path.join(d, "account.json")
        if not os.path.exists(aj):
            continue
        try:
            with open(aj, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue
        uid = meta.get("uid") or name
        # 目录名本身就是 32 位 uid 时以目录名为准：数据目录改过名之后，
        # 里面的 account.json 可能还写着旧 uid，照它补登会凭空多一个「幽灵账号」
        if re.fullmatch(r"[0-9a-f]{32}", name) and uid != name:
            uid = name
        if uid in known_uids or is_ghost(uid):
            continue
        port = _first_free_port(set(used))
        used.append(port)
        out.append({"name": "账号 %s" % uid[:8], "uid": uid, "port": port,
                    "profile": make_profile(uid[:8]), "cookie_src": None,
                    "note": "从已有数据目录自动补登，需重新扫码认领"})
    return out


def _first_free_port(taken):
    for p in range(9222, 9399):
        if p in taken or _port_busy(p):
            continue
        return p
    raise RuntimeError("9222-9398 端口都被占用了，清理一下再试")


def real_accounts():
    """正式账号，按端口升序（主号在前）。等价 all() 的有序版。"""
    accs = all()
    try:
        accs.sort(key=lambda a: int(a.get("port") or 9999))
    except Exception:
        pass
    return accs


def main_uid():
    """主账号 uid = 正式账号里端口最小的那个（通常 9222）。

    取代散落各处的硬编码 uid：注册表为空时返回空串，调用方自己决定兜底。
    """
    accs = real_accounts()
    return accs[0].get("uid") if accs else ""


def default_uid():
    """CLI / 默认值用：主账号 uid，没有就用当前账号。"""
    return main_uid() or get_current_uid()


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


def remove_by_port(port):
    """待登录槽位 uid 是空的，只能按端口移除。"""
    o = _load()
    n = len(o["accounts"])
    o["accounts"] = [a for a in o["accounts"]
                     if int(a.get("port") or 0) != int(port)]
    if len(o["accounts"]) == n:
        return False
    _save(o)
    return True


def register(name="", note=""):
    """新建一条「待登录」账号：先分配端口和独立 profile，uid 等登录后回填。

    为什么要先分配：扫码登录得先知道"往哪个浏览器里登"，
    而浏览器是靠端口+profile 定位的。所以顺序必然是 先建槽位 → 再登录 → 再回填 uid。
    """
    port = next_port()
    prof = make_profile("acct%d" % port)
    acc = {"name": name or ("新账号 %d" % port), "uid": "", "port": port,
           "profile": prof, "cookie_src": None, "note": note,
           "added": datetime.date.today().isoformat(),
           "order": len(_load()["accounts"])}
    o = _load()
    o["accounts"].append(acc)
    _save(o)
    return acc


def set_uid(old_uid, new_uid, name=None):
    """登录成功后回填 uid。

    优先按 old_uid 定向更新；否则认领第一个空 uid 的槽位（"待登录"那条）；
    都没有就作为新账号登记。这样"先配端口后登录"和"直接登录"两条路都能走通。
    """
    o = _load()
    for a in o["accounts"]:
        if old_uid and a.get("uid") == old_uid:
            a["uid"] = new_uid
            if name:
                a["name"] = name
            _save(o)
            return a
    for a in o["accounts"]:
        if not a.get("uid"):
            a["uid"] = new_uid
            if name:
                a["name"] = name
            _save(o)
            return a
    return upsert({"uid": new_uid, "name": name or ("账号 %s" % new_uid[:8])})


def set_order(uids):
    """按给定顺序写 order 字段（账号卡片拖动排序）。"""
    o = _load()
    pos = {u: i for i, u in enumerate(uids)}
    tail = len(pos) + 1
    for a in o["accounts"]:
        a["order"] = pos.get(a.get("uid"), tail)
    _save(o)
    return entries()


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


# ---------------- 账号数据概要 ----------------
def data_dir(uid):
    return account_dir(uid) if uid else os.path.join(accounts_dir(), "_pending")


def stats(uid):
    """读该账号已抓到的数据概要：曲库数 / 最近一日播放合计 / 数据日期。

    ★ 两个账号导出的 CSV 列名并不统一，所以按候选列名依次尝试，不能写死：
        主账号  ：当日播放量 / 最近一日播放 / 最近一日日期 / 累计播放量
        第二账号：昨日汽水播放量 / 数据日期
    另外 uid 为空（还没登录的槽位）时**必须**直接返回空，
    不能走 data_dir("") 回退到别的目录——否则新账号会显示成另一个账号的曲库。
    """
    out = {"total": 0, "plays": 0, "date": "", "has_data": False}
    if not uid:
        return out
    d = data_dir(uid)
    aj = os.path.join(d, "account.json")
    if os.path.exists(aj):
        try:
            with io.open(aj, encoding="utf-8") as f:
                out["total"] = int(json.load(f).get("total") or 0)
        except Exception:
            pass
    csv_path = os.path.join(d, "汽水昨日数据.csv")
    if not os.path.exists(csv_path):
        return out
    out["has_data"] = True
    play_cols = ("昨日汽水播放量", "最近一日播放", "当日播放量")
    date_cols = ("最近一日日期", "数据日期")
    try:
        with io.open(csv_path, encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            cols = rd.fieldnames or []
            pc = next((c for c in play_cols if c in cols), None)
            dc = next((c for c in date_cols if c in cols), None)
            tot, date = 0, ""
            for row in rd:
                if pc:
                    try:
                        tot += int(float(row.get(pc) or 0))
                    except Exception:
                        pass
                if not date and dc:
                    date = (row.get(dc) or "").strip()
            out["plays"] = tot
            out["date"] = date
    except Exception:
        pass
    return out

# -*- coding: utf-8 -*-
"""updater.py —— 历史版本 + 热更新（宿主和每个插件共用同一套）

为什么放在 core
    主机要能更新自己，冷启动插件也要能更新自己，看板插件也要。
    如果每个插件各写一份，就会出现三套逻辑、三种坑。所以统一在这里，
    谁想更新谁 `import qishui_core.updater`。

两条能力
    1) 历史版本：每次装新版本，旧的先归档到 `<目标目录>/_versions/<ver>/`，
       保留最近 KEEP_VERSIONS 份，可一键回滚。归档是**整目录 copy**，
       不是 git，不依赖网络，断网也能回滚。
    2) 热更新：读清单（本地 update.json 或远程 feed）→ 比对版本 → 下载 →
       校验 sha256 → 解到 `_staged/<ver>/` → 由外部小脚本换目录（Windows 上
       运行中的 exe/dll 换不掉，必须在进程退出后由别的进程干）。

清单格式（update.json，放在发布目录或 GitHub raw 上）
    {
      "id": "coldstart",
      "channel": "stable",
      "latest": "1.2.0",
      "releases": [
        {"version": "1.2.0", "date": "2026-11-05",
         "notes": "修 xxx", "url": "https://.../coldstart-1.2.0.zip",
         "sha256": "....", "size": 1234567},
        {"version": "1.1.0", "date": "2026-10-20", "notes": "...", "url": "..."}
      ]
    }

    多版本清单是刻意的——用户可能想装回旧版，也可能被 pinned 在某版，
    所以 `releases` 是个列表而不是单个 latest。

命令行
    python -m qishui_core.updater check   --id coldstart --feed <url>
    python -m qishui_core.updater list    --id coldstart
    python -m qishui_core.updater stage   --id coldstart --ver 1.2.0 --feed <url>
    python -m qishui_core.updater apply   --id coldstart
    python -m qishui_core.updater rollback --id coldstart --ver 1.1.0
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

from .paths import DATA_ROOT, plugin_data

KEEP_VERSIONS = 4          # 每个目标保留几份历史
CHUNK = 1024 * 256


# ----------------------------------------------------------------- 工具
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(msg):
    try:
        print("[updater] %s" % msg, flush=True)
    except Exception:
        pass


def _vtuple(v):
    """"1.2.10" → (1,2,10)，用于比较；非法片段按 0 算。"""
    out = []
    for seg in str(v or "0").replace("-", ".").split("."):
        m = re.match(r"^(\d+)", seg)
        out.append(int(m.group(1)) if m else 0)
    return tuple(out)


def newer(a, b):
    """a 是否比 b 新。"""
    return _vtuple(a) > _vtuple(b)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ----------------------------------------------------------------- 状态文件
def _state_file(target_id):
    return os.path.join(plugin_data(target_id), "versions.json")


def load_state(target_id):
    try:
        with io.open(_state_file(target_id), encoding="utf-8") as f:
            o = json.load(f)
        if isinstance(o, dict):
            o.setdefault("id", target_id)
            o.setdefault("current", "")
            o.setdefault("history", [])
            return o
    except Exception:
        pass
    return {"id": target_id, "current": "", "history": []}


def save_state(target_id, st):
    p = _state_file(target_id)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


# ----------------------------------------------------------------- 历史版本
def target_root(target_id, install_dir=None):
    """目标本体所在目录。install_dir 为空时用插件目录约定。"""
    if install_dir:
        return os.path.abspath(install_dir)
    return os.path.join(DATA_ROOT, "plugins", str(target_id))


def versions_dir(target_id, install_dir=None):
    """历史版本放**插件目录外面**（plugin_data/<pid>/_versions/）。

    放里面看着整齐，但换目录脚本是整目录搬走、卸载也是整目录搬走——
    历史会跟着一起没。放外面才真的能回滚。
    """
    p = os.path.join(plugin_data(target_id), "_versions")
    os.makedirs(p, exist_ok=True)
    return p


def snapshot(target_id, version, install_dir=None, note=""):
    """把当前目录归档成一份历史版本。"""
    src = target_root(target_id, install_dir)
    if not os.path.isdir(src):
        return None
    dst = os.path.join(versions_dir(target_id, install_dir), str(version))
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        "_versions", "_staged", "__pycache__", "*.pyc", "_update"))
    st = load_state(target_id)
    st["current"] = str(version)
    st["history"] = [h for h in st["history"] if h.get("version") != str(version)]
    st["history"].insert(0, {"version": str(version), "at": _now(),
                             "note": note or "", "dir": dst})
    st["history"] = st["history"][:KEEP_VERSIONS]
    save_state(target_id, st)
    _prune(target_id, install_dir)
    _log("已归档版本 %s → %s" % (version, dst))
    return dst


def _prune(target_id, install_dir=None):
    """只留 KEEP_VERSIONS 份历史目录。"""
    st = load_state(target_id)
    keep = {h.get("dir") for h in st["history"]}
    vd = versions_dir(target_id, install_dir)
    for name in sorted(os.listdir(vd)):
        p = os.path.join(vd, name)
        if os.path.isdir(p) and p not in keep:
            shutil.rmtree(p, ignore_errors=True)


def list_versions(target_id, install_dir=None):
    st = load_state(target_id)
    return [{"version": h.get("version"), "at": h.get("at"),
             "note": h.get("note", ""), "exists": os.path.isdir(h.get("dir", ""))}
            for h in st.get("history", [])]


def rollback(target_id, version, install_dir=None):
    """把历史版本整目录换回来（保留当前版本一份副本，防止回滚也回不去）。"""
    src = os.path.join(versions_dir(target_id, install_dir), str(version))
    if not os.path.isdir(src):
        raise RuntimeError("历史版本不存在：%s" % src)
    dst = target_root(target_id, install_dir)
    bak = dst + ".bak-%d" % int(time.time())
    if os.path.isdir(dst):
        shutil.move(dst, bak)
    shutil.copytree(src, dst)
    st = load_state(target_id)
    st["current"] = str(version)
    save_state(target_id, st)
    _log("已回滚到 %s（原目录备份在 %s）" % (version, bak))
    return dst


# ----------------------------------------------------------------- 清单
def fetch_manifest(feed):
    """读更新清单。feed 可以是 http(s) URL，也可以是本地文件路径。"""
    feed = (feed or "").strip()
    if not feed:
        return None
    if feed.lower().startswith(("http://", "https://")):
        import urllib.request
        req = urllib.request.Request(feed, headers={"User-Agent": "qishui-updater/1"})
        raw = urllib.request.urlopen(req, timeout=30).read()
        return json.loads(raw.decode("utf-8"))
    with io.open(feed, encoding="utf-8") as f:
        return json.load(f)


def pick_release(manifest, version=None):
    """从清单里挑一条：给了 version 就按版本找，否则取最新。"""
    rels = (manifest or {}).get("releases") or []
    if not rels and (manifest or {}).get("latest"):
        rels = [{"version": manifest["latest"]}]
    if not rels:
        return None
    if version:
        for r in rels:
            if str(r.get("version")) == str(version):
                return r
        return None
    return sorted(rels, key=lambda r: _vtuple(r.get("version")))[-1]


# ----------------------------------------------------------------- 下载/解压
def download(url, out_path, expect_sha="", on_progress=None):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if url.lower().startswith(("http://", "https://")):
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "qishui-updater/1"})
        total, done = 0, 0
        with urllib.request.urlopen(req, timeout=120) as r:
            try:
                total = int(r.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            with open(out_path, "wb") as f:
                while True:
                    b = r.read(CHUNK)
                    if not b:
                        break
                    f.write(b)
                    done += len(b)
                    if on_progress:
                        on_progress(done, total)
    else:
        shutil.copy2(url, out_path)
    got = sha256_of(out_path)
    if expect_sha and got.lower() != str(expect_sha).lower():
        os.remove(out_path)
        raise RuntimeError("校验失败：期望 %s，实际 %s" % (expect_sha, got))
    return got


def stage(target_id, ver, url, expect_sha="", on_progress=None):
    """下载并解到一个待装目录，不动线上版本。"""
    base = os.path.join(plugin_data(target_id), "_update")
    ext = os.path.splitext(url.split("?")[0])[1] or ".zip"
    zpath = os.path.join(base, "%s-%s%s" % (target_id, ver, ext))
    download(url, zpath, expect_sha, on_progress)
    out = os.path.join(plugin_data(target_id), "_staged", str(ver))
    if os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, exist_ok=True)
    if zipfile.is_zipfile(zpath):
        with zipfile.ZipFile(zpath) as z:
            z.extractall(out)
    else:
        shutil.copy2(zpath, out)
    _flatten(out)
    st = load_state(target_id)
    st["staged"] = {"version": str(ver), "dir": out, "at": _now()}
    save_state(target_id, st)
    _log("已就绪：%s（%s）" % (ver, out))
    return out


def _flatten(root):
    """压缩包里如果只有一层同名目录，把内容提上来，避免多套一层。"""
    try:
        items = [n for n in os.listdir(root) if n not in ("__MACOSX",)]
        if len(items) == 1:
            inner = os.path.join(root, items[0])
            if os.path.isdir(inner):
                for n in os.listdir(inner):
                    shutil.move(os.path.join(inner, n), os.path.join(root, n))
                os.rmdir(inner)
    except Exception:
        pass


# ----------------------------------------------------------------- 应用（换目录）
SWAP_PY = r'''# -*- coding: utf-8 -*-
"""一次性换目录脚本，由 updater 生成，跑完自删。"""
import os, sys, time, shutil, subprocess

src, dst, relaunch = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
wait_pid = int(sys.argv[4]) if len(sys.argv) > 4 else 0

def alive(pid):
    if not pid:
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259
    except Exception:
        return False

t0 = time.time()
while alive(wait_pid) and time.time() - t0 < 120:
    time.sleep(0.5)
time.sleep(1.0)

bak = dst + ".old-%d" % int(time.time())
try:
    if os.path.isdir(dst):
        os.rename(dst, bak)
    shutil.copytree(src, dst)
except Exception as e:
    sys.stderr.write("swap failed: %s\n" % e)
    if os.path.isdir(bak) and not os.path.isdir(dst):
        os.rename(bak, dst)
    sys.exit(2)

if relaunch and os.path.isfile(relaunch):
    try:
        subprocess.Popen([relaunch], close_fds=True)
    except Exception:
        pass
try:
    os.remove(os.path.abspath(__file__))
except Exception:
    pass
'''


def apply_staged(target_id, install_dir=None, relaunch="", wait_pid=0, dry=False):
    """把 _staged/<ver> 换到线上目录。Windows 上必须进程退出后执行。"""
    st = load_state(target_id)
    s = st.get("staged") or {}
    src = s.get("dir") or ""
    ver = s.get("version") or ""
    if not src or not os.path.isdir(src):
        raise RuntimeError("没有待装版本，先 stage()")
    dst = target_root(target_id, install_dir)
    if dry:
        _log("[dry] %s → %s（等 PID %s 退出）" % (src, dst, wait_pid))
        return {"would_swap": [src, dst]}

    # 归档现在的版本，留作回滚
    cur = st.get("current") or ""
    if cur and os.path.isdir(dst):
        try:
            snapshot(target_id, cur, install_dir, note="自动归档")
        except Exception:
            pass

    sp = os.path.join(DATA_ROOT, "_swap_%s_%d.py" % (target_id, int(time.time())))
    with io.open(sp, "w", encoding="utf-8") as f:
        f.write(SWAP_PY)
    args = [sys.executable, sp, src, dst, relaunch, str(int(wait_pid or 0))]
    if getattr(sys, "frozen", False):
        args = [sys.executable, sp, src, dst, relaunch, str(int(wait_pid or 0))]
    DETACHED = 0x00000008 | 0x00000200
    subprocess.Popen(args, close_fds=True,
                     creationflags=DETACHED if os.name == "nt" else 0)
    st["current"] = str(ver)
    st["staged"] = {}
    st["pending"] = {"version": str(ver), "at": _now()}
    save_state(target_id, st)
    _log("换目录脚本已启动：%s" % sp)
    return {"swap_script": sp, "version": ver}


# ----------------------------------------------------------------- 一站式
def check(target_id, feed, current_version=""):
    m = fetch_manifest(feed)
    if not m:
        return None
    latest = pick_release(m)
    if not latest:
        return None
    st = load_state(target_id)
    cur = current_version or st.get("current") or ""
    return {
        "id": target_id,
        "current": cur,
        "latest": latest.get("version"),
        "update_available": (not cur) or newer(latest.get("version"), cur),
        "release": latest,
        "notes": latest.get("notes", ""),
    }


def update(target_id, feed, current_version="", install_dir=None,
           relaunch="", on_progress=None):
    """check → stage，返回待装信息；真要生效在下次启动前调 apply_staged。"""
    info = check(target_id, feed, current_version)
    if not info:
        raise RuntimeError("清单为空：%s" % feed)
    if not info["update_available"]:
        return {"up_to_date": True, "version": info["current"]}
    rel = info["release"]
    if not rel.get("url"):
        raise RuntimeError("清单里 %s 没有 url" % info["latest"])
    d = stage(target_id, rel["version"], rel["url"], rel.get("sha256", ""),
              on_progress)
    info["staged"] = d
    info["install_dir"] = target_root(target_id, install_dir)
    info["relaunch"] = relaunch
    return info


# ----------------------------------------------------------------- CLI
def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="qishui_core.updater")
    ap.add_argument("cmd", choices=["check", "list", "stage", "apply",
                                    "rollback", "snapshot"])
    ap.add_argument("--id", required=True)
    ap.add_argument("--feed", default="")
    ap.add_argument("--ver", default="")
    ap.add_argument("--dir", default="")
    ap.add_argument("--cur", default="")
    ap.add_argument("--relaunch", default="")
    ap.add_argument("--pid", type=int, default=0)
    a = ap.parse_args(argv)

    if a.cmd == "check":
        print(json.dumps(check(a.id, a.feed, a.cur), ensure_ascii=False, indent=2))
    elif a.cmd == "list":
        print(json.dumps(load_state(a.id), ensure_ascii=False, indent=2))
        print(json.dumps(list_versions(a.id, a.dir), ensure_ascii=False, indent=2))
    elif a.cmd == "stage":
        m = fetch_manifest(a.feed)
        rel = pick_release(m, a.ver)
        if not rel:
            raise SystemExit("清单里没有版本 %s" % a.ver)
        print(stage(a.id, rel["version"], rel["url"], rel.get("sha256", ""),
                    lambda d, t: None))
    elif a.cmd == "apply":
        print(apply_staged(a.id, a.dir, a.relaunch, a.pid))
    elif a.cmd == "rollback":
        print(rollback(a.id, a.ver, a.dir))
    elif a.cmd == "snapshot":
        print(snapshot(a.id, a.ver or _now()[:10], a.dir))


if __name__ == "__main__":
    _cli(sys.argv[1:])

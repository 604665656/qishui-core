# -*- coding: utf-8 -*-
"""schedule.py —— 每日定时（进程内跑 + 可选写进系统计划任务）

为什么有两层
    插件被宿主加载时，宿主一直开着，进程内定时器就够了，还能拿到界面、
    能弹通知，体验最好。但用户关了宿主就没了，所以「关机也要跑」的任务
    得落到 Windows 计划任务上。两层各管一段，不打架：
      - 进程内：宿主开着时准点跑（TICK 秒粒度）
      - 计划任务：宿主没开时由系统拉起一个 `--task <id>` 的无界面进程

去重
    同一任务在进程内和计划任务里可能同时到点。用 `_schedule.json` 里记的
    last_run（按天）做闸门——当天跑过就跳过，谁先到谁算。

用法
    s = DailyScheduler()
    s.add("coldstart", run_fn, "07:30", owner="coldstart")
    s.start()            # 起一个后台线程
    ...
    s.stop()

    s.register_os_task("coldstart", '"C:\\\\...\\\\host.exe" --task coldstart', "07:30")
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time

from .paths import DATA_ROOT

REG = os.path.join(DATA_ROOT, "_schedule.json")
TICK = 20                    # 每 20 秒看一次表，够用且几乎不吃 CPU
GRACE_MIN = 180              # 到点后多少分钟内还算「今天该跑」（错过太久就跳过）
TASK_PREFIX = "QishuiTask_"
_SCHTASKS = None             # schtasks 可用性缓存：None=没探过


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return time.strftime("%Y-%m-%d")


def load():
    try:
        with io.open(REG, encoding="utf-8") as f:
            o = json.load(f)
        if isinstance(o, dict):
            o.setdefault("tasks", {})
            return o
    except Exception:
        pass
    return {"tasks": {}}


def save(reg):
    os.makedirs(os.path.dirname(REG), exist_ok=True)
    tmp = REG + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REG)


def hm_valid(hm):
    try:
        h, m = str(hm).split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        return False


def _minutes(hm):
    h, m = str(hm).split(":")
    return int(h) * 60 + int(m)


# ----------------------------------------------------------------- 进程内
class DailyScheduler(object):
    def __init__(self, log=print):
        self._fns = {}
        self._thread = None
        self._stop = threading.Event()
        self.log = log or (lambda *a: None)

    # ---- 注册 ----
    def add(self, task_id, fn, hm="07:30", owner="", title=""):
        if not hm_valid(hm):
            raise ValueError("时间格式要 HH:MM：%r" % (hm,))
        reg = load()
        t = reg["tasks"].get(task_id) or {}
        t.update({"hm": hm, "owner": owner or task_id, "title": title,
                  "enabled": True, "updated": _now()})
        reg["tasks"][task_id] = t
        save(reg)
        self._fns[task_id] = fn
        return t

    def remove(self, task_id):
        reg = load()
        reg["tasks"].pop(task_id, None)
        save(reg)
        self._fns.pop(task_id, None)
        return True

    def set_time(self, task_id, hm):
        if not hm_valid(hm):
            raise ValueError("时间格式要 HH:MM")
        reg = load()
        if task_id not in reg["tasks"]:
            return False
        reg["tasks"][task_id]["hm"] = hm
        reg["tasks"][task_id]["updated"] = _now()
        save(reg)
        return True

    def tasks(self):
        return load().get("tasks", {})

    # ---- 跑 ----
    def due(self, task, now=None):
        """现在是不是该跑（当天没跑过、已过点、且没过太久）。返回 (bool, 原因)。"""
        now = now or time.localtime()
        hm = task.get("hm") or ""
        if not hm_valid(hm):
            return False, "时间无效"
        if not task.get("enabled", True):
            return False, "已停用"
        if task.get("last_run") == _today():
            return False, "今天已跑"
        cur = now.tm_hour * 60 + now.tm_min
        tgt = _minutes(hm)
        if cur < tgt:
            return False, "还没到点"
        if cur - tgt > GRACE_MIN:
            return False, "已错过窗口"
        return True, "到点"

    def run_once(self, task_id, force=False):
        reg = load()
        task = reg["tasks"].get(task_id)
        if not task:
            return {"ok": False, "why": "没这个任务"}
        if not force:
            ok, why = self.due(task)
            if not ok:
                return {"ok": False, "why": why}
        fn = self._fns.get(task_id)
        if not fn:
            return {"ok": False, "why": "没有可执行函数（需要先 add）"}
        t0 = time.time()
        try:
            fn()
            err = ""
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
        reg = load()
        t = reg["tasks"].get(task_id) or {}
        t["last_run"] = _today()
        t["last_at"] = _now()
        t["last_sec"] = round(time.time() - t0, 1)
        t["last_err"] = err
        reg["tasks"][task_id] = t
        save(reg)
        return {"ok": not err, "err": err, "sec": t["last_sec"]}

    def _loop(self):
        while not self._stop.is_set():
            try:
                for tid, task in list(self.tasks().items()):
                    if tid not in self._fns:
                        continue
                    ok, _ = self.due(task)
                    if ok:
                        self.log("定时任务到点：%s" % tid)
                        self.run_once(tid, force=True)
            except Exception as e:
                self.log("定时循环异常：%s" % e)
            self._stop.wait(TICK)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="qishui-schedule")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread = None


# ----------------------------------------------------------------- 系统计划任务
def _bat_path(task_id):
    d = os.path.join(DATA_ROOT, "_tasks")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s.bat" % task_id)


def write_bat(task_id, cmd, hm="07:30"):
    """schtasks 被组策略/安全软件拦掉时的兜底：生成一个双击就注册的 bat。

    ★ 必须是 CRLF，否则 cmd.exe 会把参数吃错（踩过）。
    """
    p = _bat_path(task_id)
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "echo 正在注册每日任务 %s (%s) ..." % (task_id, hm),
        'schtasks /Create /TN "%s%s" /TR "%s" /SC DAILY /ST %s /F'
        % (TASK_PREFIX, task_id, cmd.replace('"', '\\"'), hm),
        "if errorlevel 1 (echo [失败] 可能需要以管理员身份运行) else (echo [成功] 已注册)",
        "pause",
    ]
    with open(p, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return p


def _schtasks_ok():
    """探一次 schtasks 能不能用，结果缓存。

    有些环境（组策略、安全软件、沙箱）根本不让调 schtasks，每次调都要等十几秒
    再报一个错。探一次记住，后面直接走 .bat 兜底，界面不卡。
    """
    global _SCHTASKS
    if _SCHTASKS is not None:
        return _SCHTASKS
    if os.name != "nt":
        _SCHTASKS = False
        return False
    try:
        r = subprocess.run(["schtasks", "/Query", "/?"], capture_output=True,
                           timeout=8, creationflags=0x08000000)
        _SCHTASKS = r.returncode == 0
    except Exception:
        _SCHTASKS = False
    return _SCHTASKS


def register_os_task(task_id, cmd, hm="07:30"):
    """写 Windows 计划任务。失败不抛异常，返回 need_manual + bat 路径。"""
    if os.name != "nt":
        return {"ok": False, "why": "仅支持 Windows", "need_manual": False}
    name = TASK_PREFIX + task_id
    if not _schtasks_ok():
        bat = write_bat(task_id, cmd, hm)
        return {"ok": False, "why": "本机不允许程序调用 schtasks",
                "need_manual": True, "bat": bat}
    args = ["schtasks", "/Create", "/TN", name, "/TR", cmd,
            "/SC", "DAILY", "/ST", hm, "/F"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30,
                           creationflags=0x08000000)
        if r.returncode == 0:
            return {"ok": True, "name": name, "hm": hm}
        why = (r.stderr or r.stdout or "").strip()[:300]
    except Exception as e:
        why = "%s: %s" % (type(e).__name__, e)
    bat = write_bat(task_id, cmd, hm)
    return {"ok": False, "why": why, "need_manual": True, "bat": bat}


def remove_os_task(task_id):
    if os.name != "nt" or not _schtasks_ok():
        return {"ok": False, "why": "本机不允许调用 schtasks"}
    name = TASK_PREFIX + task_id
    try:
        r = subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"],
                           capture_output=True, text=True, timeout=30,
                           creationflags=0x08000000)
        ok = r.returncode == 0
        return {"ok": ok, "why": "" if ok else (r.stderr or "")[:200]}
    except Exception as e:
        return {"ok": False, "why": str(e)}


def os_task_state(task_id):
    if os.name != "nt" or not _schtasks_ok():
        return None
    name = TASK_PREFIX + task_id
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", name, "/FO", "LIST"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=0x08000000)
        if r.returncode != 0:
            return None
        info = {}
        for line in (r.stdout or "").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip()
        return info
    except Exception:
        return None


if __name__ == "__main__":
    s = DailyScheduler(log=print)
    for tid, t in s.tasks().items():
        print("%-16s %s  last=%s  %s" % (tid, t.get("hm"), t.get("last_run", "-"),
                                         t.get("last_err", "")))

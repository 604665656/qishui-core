# -*- coding: utf-8 -*-
"""launcher.py —— 用独立 profile 拉起一个带 CDP 调用的浏览器

为什么每个账号要一套 profile
---------------------------
同一个 `--user-data-dir` 只能被一个浏览器实例占用，而且登录态是 profile 级的：
两个账号挤一个 profile，后登的会把先登的顶掉。所以"一台电脑管 N 个账号"的
物理基础就是 **N 个 profile + N 个调试端口**。

为什么窗口挪到屏幕外
-------------------
自动化操作全部走 CDP，窗口在不在前台完全无所谓。但用户不希望"程序在后台出歌时
屏幕被弹窗打断"，所以默认 `--window-position=-32000,-32000`。需要看页面时
（比如手工填验证码），从任务栏点出来即可。

安全上的一个细节
---------------
`--remote-allow-origins` 早先写的是 `*`，等于让本机任意网页都能用 JS 连上调试端口
读 cookie、点按钮。这里收紧成只放本机来源，需要额外来源时用环境变量
`QISHUI_CDP_ORIGINS` 追加（逗号分隔）。cdp_lite 是直连 WS、不带 Origin 头，
所以收紧白名单不影响自身连接。
"""
from __future__ import annotations

import os
import subprocess
import time

from . import auth

LOGIN_URL = "https://music.douyin.com/"


def cdp_origins(port):
    """CDP 允许的跨源来源：只放本机回环。"""
    base = ["http://127.0.0.1:%d" % int(port),
            "http://localhost:%d" % int(port),
            "http://127.0.0.1", "http://localhost"]
    extra = (os.environ.get("QISHUI_CDP_ORIGINS") or "").strip()
    if extra:
        base += [x.strip() for x in extra.split(",") if x.strip()]
    return ",".join(base)


def launch_account(port, profile_dir, open_url=LOGIN_URL, timeout=20,
                   exe=None, hide_window=True):
    """用独立 profile 拉起一个带 CDP 的浏览器，打开登录页。返回 (ok, msg)。

    端口已被占用时不算失败的前提是：占用的那个确实是 CDP
    （说明这个账号的浏览器早就开着），此时直接返回成功。
    """
    port = int(port)
    exe = exe or auth.find_edge()
    if not exe:
        return False, "未在本机找到 Edge / Chrome 等 Chromium 内核浏览器"

    try:
        os.makedirs(profile_dir, exist_ok=True)
    except Exception as e:
        return False, "建 profile 目录失败：%s" % e

    if auth.port_busy(port):
        if auth.discover_ws(port):
            return True, "端口 %d 上已有 CDP 浏览器在运行" % port
        return False, "端口 %d 已被其它程序占用，换一个端口" % port

    args = [exe,
            "--remote-debugging-port=%d" % port,
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=%s" % cdp_origins(port),
            "--user-data-dir=%s" % profile_dir,
            "--no-first-run", "--no-default-browser-check"]
    if hide_window:
        args += ["--window-position=-32000,-32000", "--window-size=1400,900"]
    args.append(open_url)

    try:
        proc = subprocess.Popen(args)
    except Exception as e:
        return False, "启动失败：%s" % e

    for _ in range(max(1, timeout)):
        time.sleep(1)
        if auth.discover_ws(port):
            return True, "已拉起（pid=%s），请在浏览器里扫码登录 %s" % (proc.pid, open_url)
    try:
        proc.terminate()
    except Exception:
        pass
    return False, "启动超时（%ds 内未连上 CDP）" % timeout


def kill_port(port, timeout=8):
    """把这个端口的浏览器关掉（换 profile / 重置登录态时会用到）。"""
    port = int(port)
    if not auth.port_busy(port):
        return True
    cdp = auth.http_get_json("/json/version", port, timeout=2) or {}
    # 拿不到 Browser.close 就走系统的端口→PID 反查
    try:
        browsers = auth.http_get_json("/json/version", port, timeout=2) or {}
        if browsers:
            from .cdp_lite import CDP
            c = CDP(browsers["webSocketDebuggerUrl"], timeout=5)
            c.call("Browser.close", t=5)
            c.close()
    except Exception:
        pass
    for _ in range(int(timeout * 2)):
        if not auth.port_busy(port):
            return True
        time.sleep(0.5)
    return not auth.port_busy(port)

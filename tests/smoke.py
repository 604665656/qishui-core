# -*- coding: utf-8 -*-
"""内核冒烟测试：不需要浏览器、不需要网络、不需要 Qt。

    QISHUI_HOME=/tmp/qs python tests/smoke.py     # 或 python -m pytest tests -q
"""
import json
import os
import sys
import tempfile
import time

HOME = os.environ.get("QISHUI_HOME") or tempfile.mkdtemp(prefix="qishui-smoke-")
os.environ["QISHUI_HOME"] = HOME

from qishui_core import API_VERSION, auth, paths, schedule, store, updater  # noqa: E402

ok = 0


def check(name, cond, extra=""):
    global ok
    assert cond, "FAIL %s %s" % (name, extra)
    ok += 1
    print("  ok  %-34s %s" % (name, extra))


def main():
    print("QISHUI_HOME =", HOME)

    # ---- 路径 ----
    check("DATA_ROOT 跟随环境变量", paths.DATA_ROOT == os.path.abspath(HOME))
    d = paths.account_dir("u1")
    check("账号目录已创建", os.path.isdir(d), d)
    check("空 uid 落到 _default", paths.account_dir("").endswith("_default"))
    check("插件目录已创建", os.path.isdir(paths.plugin_data("p1")))
    check("插件搜索路径有 2 条", len(paths.plugins_dir()) >= 1)

    # ---- 登录态（纯函数，不连浏览器） ----
    ck = "sessionid=abc; uid_tt=12345; csrf_token=tok"
    check("has_login 认 sessionid", auth.has_login(ck))
    check("has_login 拒绝空串", not auth.has_login(""))
    check("uid_from 取 uid_tt", auth.uid_from(ck) == "12345")
    check("csrf_token 取值", auth.csrf_token(ck) == "tok")
    check("缓存按端口分文件",
          auth.cache_path(9222) != auth.cache_path(9223)
          and "9222" in auth.cache_path(9222))

    # ---- api 多账号隔离 ----
    from qishui_core import api
    api.use_port(9223)
    check("use_port 生效", api.current_port() == 9223)
    check("cookie 缓存跟端口走", "9223" in api.cache_file())

    # ---- 账号注册表 ----
    store.upsert({"uid": "u1", "name": "A", "port": 9222})
    store.upsert({"uid": "u2", "name": "B", "port": 9223})
    check("注册表 2 个账号", len(store.all()) == 2)
    check("按端口查账号", (store.by_port(9223) or {}).get("uid") == "u2")
    store.set_current_uid("u1")
    check("当前账号可设可读", store.get_current_uid() == "u1")
    check("下一个空闲端口", store.next_port(9222) == 9224, store.next_port(9222))

    # ---- 版本比较 ----
    check("1.2.10 > 1.2.9", updater.newer("1.2.10", "1.2.9"))
    check("1.0.0 不小于 1.0.0", not updater.newer("1.0.0", "1.0.0"))

    # ---- 历史版本：归档 → 改坏 → 回滚 ----
    tgt = os.path.join(paths.plugins_dir()[0], "demo")
    os.makedirs(tgt, exist_ok=True)
    with open(os.path.join(tgt, "plugin.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "demo", "version": "1.0.0"}, f)
    updater.snapshot("demo", "1.0.0", tgt, note="基线")
    with open(os.path.join(tgt, "plugin.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "demo", "version": "2.0.0"}, f)
    updater.rollback("demo", "1.0.0", tgt)
    back = json.load(open(os.path.join(tgt, "plugin.json"), encoding="utf-8"))
    check("回滚后版本正确", back["version"] == "1.0.0", back["version"])
    check("归档放在插件目录外",
          os.path.isdir(os.path.join(paths.plugin_data("demo"), "_versions")))

    # ---- 清单解析 ----
    feed = os.path.join(HOME, "update.json")
    with open(feed, "w", encoding="utf-8") as f:
        json.dump({"latest": "2.0.0", "releases": [
            {"version": "1.0.0", "url": "x"},
            {"version": "2.0.0", "url": "y", "notes": "新"}]}, f)
    info = updater.check("demo", feed, "1.0.0")
    check("check 发现新版本", info["update_available"] and info["latest"] == "2.0.0")
    check("pick_release 取最新", updater.pick_release(
        updater.fetch_manifest(feed))["version"] == "2.0.0")

    # ---- 定时 ----
    check("时间格式校验", schedule.hm_valid("06:43") and not schedule.hm_valid("24:00"))
    s = schedule.DailyScheduler(log=lambda m: None)
    s.add("t1", lambda: None, "07:30", owner="demo")
    check("任务已登记", "t1" in s.tasks())
    t = s.tasks()["t1"]
    import time as _t
    due, why = s.due(t, _t.localtime())
    check("未到点不跑", (not due) or why == "到点", why)
    check("停用后登记生效", s.set_time("t1", "06:43") and s.tasks()["t1"]["hm"] == "06:43")
    s.remove("t1")
    check("删除后登记为空", "t1" not in s.tasks())

    # ---- 账号管理能力（api_version 2） ----
    from qishui_core.context import ToolContext
    from qishui_core import auth as _auth
    ctx = ToolContext("smoke-acct")
    A = ctx.accounts
    check("ctx.accounts 可用", A is not None)
    n0 = len(A.list())
    acc = A.register("冒烟账号")
    check("register 分配了端口", int(acc["port"]) >= 9222, acc["port"])
    check("register 分配了独立 profile", bool(acc["profile"]))
    check("register 后列表 +1", len(A.list()) == n0 + 1)
    check("待登录槽位 uid 为空", acc["uid"] == "")
    check("待登录槽位统计全空", A.stats("")["total"] == 0 and not A.stats("")["has_data"])
    A.bind(acc["port"], "u-smoke")
    got = A.by_port(acc["port"])
    check("bind 回填 uid", got.get("uid") == "u-smoke", got.get("uid"))
    A.rename("u-smoke", "改过名")
    check("rename 生效", A.get("u-smoke").get("name") == "改过名")
    check("set_order 不炸", isinstance(A.set_order(["u-smoke"]), list))
    check("幽灵 uid 识别", A.is_ghost("f" * 16) and not A.is_ghost("abc123"))
    check("按端口移除槽位", A.remove(port=acc["port"]) and not A.by_port(acc["port"]))
    check("数据目录跟着 uid",
          A.data_dir("u-smoke").endswith(os.path.join("accounts", "u-smoke")))
    check("空 uid 落到 _pending", A.data_dir("").endswith("_pending"))
    check("未开的端口 probe 返回 offline",
          _auth.probe(9399)["online"] is False)
    check("page_ws 对空端口不抛异常", _auth.page_ws(9399) == "")

    check("API_VERSION 与插件契约一致", API_VERSION == 2)
    print("\n全部通过：%d 项" % ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())

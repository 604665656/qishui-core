#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CDP 轻封装：eval / 鼠标 / 键盘 / 截图，供各探针与正式脚本复用"""
import json, time, base64, os


class CDP:
    def __init__(self, ws_url, timeout=25):
        import websocket  # 与 update_all 同源
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self.nid = 0
        self.timeout = timeout
        self.send("Runtime.enable")
        self.send("Page.enable")
        time.sleep(0.2)

    def send(self, method, params=None):
        self.nid += 1
        i = self.nid
        self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        return i

    def recv_id(self, want, t=None):
        t = t or self.timeout
        end = time.time() + t
        while time.time() < end:
            # 关键：socket 自己的 timeout 才是真正能中断阻塞 recv 的东西。
            # 以前这里只在循环里判时间，底层 recv 仍按构造时的 25 秒死等，
            # 传 t=3 完全没用 —— 界面刷新时会把整个窗口卡住十几二十秒。
            left = max(0.2, end - time.time())
            try:
                self.ws.settimeout(min(left, t))
            except Exception:
                pass
            try:
                r = json.loads(self.ws.recv())
            except Exception:
                return None
            if r.get("id") == want:
                return r
        return None

    def call(self, method, params=None, t=None):
        return self.recv_id(self.send(method, params), t)

    def js(self, expr, t=None):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True}, t)
        res = (r or {}).get("result", {})
        if res.get("exceptionDetails"):
            d = res["exceptionDetails"].get("exception", {}).get("description", "")
            return {"__err": str(d)[:250]}
        return res.get("result", {}).get("value")

    def jsj(self, expr, t=None):
        return json.dumps(self.js(expr, t), ensure_ascii=False)

    def move(self, x, y):
        self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": int(x), "y": int(y)})
        time.sleep(0.06)

    def click(self, x, y, settle=0.35):
        self.move(x, y)
        self.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": int(x), "y": int(y),
                                               "button": "left", "clickCount": 1})
        time.sleep(0.07)
        self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": int(x), "y": int(y),
                                               "button": "left", "clickCount": 1})
        time.sleep(settle)

    def key(self, key, code=None, vk=0, text=None):
        code = code or key
        base = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        p = dict(base, type="rawKeyDown")
        if text:
            p["text"] = text
        self.send("Input.dispatchKeyEvent", p)
        time.sleep(0.05)
        self.send("Input.dispatchKeyEvent", dict(base, type="keyUp"))

    def shot(self, path):
        r = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}, 30)
        data = ((r or {}).get("result") or {}).get("data")
        if not data:
            return None
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


_QUIET = {"on": False}


def set_quiet(on=True):
    """静默模式：不把浏览器窗口/标签页抢到前台。

    为什么需要：
        出歌/发布要跑几十分钟，每步都 Page.bringToFront 会把 Edge 顶到最前面、
        在你正常用电脑时反复抢焦点（用户原话："出歌等不开窗口，集合到工作台里"）。
        实测解冻真正起作用的是 Page.setWebLifecycleState('active')，
        bringToFront 只是顺手——所以静默模式下只做前者，页面里的 fetch 照样能跑。
    """
    _QUIET["on"] = bool(on)
    return _QUIET["on"]


def quiet():
    return _QUIET["on"]


def wake(cdp):
    """把标签页从「后台冻结」里唤醒。
    浏览器把最小化/后台的标签挂起后，页面里的 fetch / setTimeout 回调不会执行——
    表现为 Runtime.evaluate 同步表达式正常，但任何 await fetch 都卡死不返回
    （我们正是被这个坑过：作品接口读不出来、生成完成判定失效）。
    Page.setWebLifecycleState('active') 实测立刻恢复。"""
    if not _QUIET["on"]:
        try:
            cdp.call("Page.bringToFront", {})
        except Exception:
            pass
    try:
        cdp.call("Page.setWebLifecycleState", {"state": "active"})
        return True
    except Exception:
        return False


def studio_ws():
    import update_all as UA
    tabs = UA.http_get_json("/json")
    for t in tabs:
        if "studio/create" in (t.get("url") or "") and t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    return None


# ---------- 可见性工具 ----------
def visible_select_text(cdp):
    """用命中测试读可见模型选择器的文本（避免读到隐藏旧节点）"""
    return cdp.js(r"""(()=>{
      const el=document.querySelector('.semi-select[class*=modelSelectionSelect]');
      if(!el)return 'no-select';
      const b=el.getBoundingClientRect();
      const x=Math.round(b.x+b.width*0.2), y=Math.round(b.y+b.height/2);
      const hit=document.elementFromPoint(x,y);
      let p=hit, sel=null;
      while(p&&p!==document.body){ if(p.classList&&p.classList.contains('semi-select')){sel=p;break;} p=p.parentElement; }
      const t=sel?(sel.innerText||'').trim():'';
      return t?t.replace(/\s+/g,' '):('miss@'+x+','+y);
    })()""")


def select_rect(cdp):
    return cdp.js(r"""(()=>{const el=document.querySelector('.semi-select[class*=modelSelectionSelect]');
      if(!el)return null;const b=el.getBoundingClientRect();
      return {x:Math.round(b.x),y:Math.round(b.y),w:Math.round(b.width),h:Math.round(b.height),
              cx:Math.round(b.x+b.width/2),cy:Math.round(b.y+b.height/2)};})()""")


def aria_expanded(cdp):
    return cdp.js(r"""(()=>{const el=document.querySelector('.semi-select[class*=modelSelectionSelect]');
      return el?el.getAttribute('aria-expanded'):'?';})()""")

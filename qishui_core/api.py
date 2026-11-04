# -*- coding: utf-8 -*-
"""
api.py —— 汽水音乐创作者后台 直连客户端（纯 HTTP；只在取 Cookie 时用 CDP）

来历
----
2026-11-03 把后台每个页面走了一遍（probe_all_api.py）+ 逐个验证参数（probe_key_api.py），
确认下面这些接口**全部不需要 msToken / a_bogus 签名**，带 cookie 直接 requests 就行。
之前「必须签名、pageSize 只能 10」的结论只对 CDP 注入 fetch 那条路成立，对直连不成立。

实测关键数据（2026-11-03，主号 许许许许许丶若辰）
  work-list   PageSize=1000 → 返回 967 条，Total=5689，6 次请求就能全量
  can-create  createDailyLimitation=10  currentSongCreateDailyCount=0
  trend       metricType 1..8 全通；scope=2 必须带 songID
  song-list/v2  songAudioType=1 → 空；=2 → 有数据（我们的是 2）

cookie 从哪来
  qishui_core.auth.cookies_from_browser(port) 走 CDP 取；本模块加了本地缓存，
  缓存新鲜时不碰浏览器，浏览器关着也能用（缓存过期才需要开浏览器刷新）。

用法
  python api_client.py quota           # 今日额度
  python api_client.py trend 2         # 汽水播放量 7 日趋势
  python api_client.py home            # 首页汇总
  python api_client.py settle          # 收入概览
  python api_client.py works 5         # 前 5 首资产
"""
import os
import sys
import json
import time
import datetime

from .paths import DATA_ROOT as BASE

COOKIE_CACHE = os.path.join(BASE, "_cookie_cache.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0")
REFERER = "https://music.douyin.com/console"
HOST = "https://music.douyin.com"

# 7 日指标的 metricType 口径（与 overview 的 8 个指标一致）
METRICS = {
    1: "汽水热度指数",
    2: "汽水播放量",
    3: "汽水付费播放指数",
    4: "汽水搜播指数",
    5: "抖音投稿量",
    6: "抖音热度指数",
    7: "抖音视频播放量",
    8: "抖音视频点赞数",
}

_CK = None


# ---------------- 当前账号端口 ----------------
# 多账号场景下，每个账号挂在不同 CDP 端口上。插件不该自己记端口，
# 统一用 use_port() 绑定当前账号，之后所有接口调用不传 port 就用这个。
_CUR_PORT = [0]        # 0 = 自动发现


def use_port(port):
    """绑定当前账号的调试端口。传 0/None 表示自动发现。"""
    global _CK
    p = int(port or 0)
    if p != _CUR_PORT[0]:
        _CK = ""                      # 换号必须丢内存里的 cookie，否则串号
    _CUR_PORT[0] = p
    return p


def current_port():
    return _CUR_PORT[0]


def _res(port):
    """把 None（=用当前绑定端口）解析成具体端口；0 表示交给 auth 自动找。"""
    return int(port) if port else int(_CUR_PORT[0] or 0)


def cache_file(port=None):
    """cookie 缓存按端口分文件，多账号互不覆盖。"""
    p = _res(port)
    if not p:
        return COOKIE_CACHE
    root, ext = os.path.splitext(COOKIE_CACHE)
    return "%s_%d%s" % (root, p, ext)


# ---------------- cookie ----------------
def cookies(port=None, max_age=6 * 3600, force=False):
    """返回 cookie 串。优先用本地缓存，过期或 force 才去浏览器取。"""
    global _CK
    port = _res(port)
    if _CK and not force:
        return _CK
    cf = cache_file(port)
    if not force and os.path.isfile(cf):
        try:
            o = json.load(open(cf, encoding="utf-8"))
            if o.get("ck") and time.time() - o.get("ts", 0) < max_age:
                _CK = o["ck"]
                return _CK
        except Exception:
            pass
    try:
        from .auth import cookies_from_browser, find_port
        port = port or find_port()
        if not port:
            raise RuntimeError("本机没有开着调试端口的浏览器")
        ck = cookies_from_browser(str(port))
    except Exception as e:
        raise RuntimeError("取 cookie 失败（浏览器需开着 %s 端口）：%s"
                           % (port or "自动发现", e))
    if not ck:
        raise RuntimeError("浏览器返回空 cookie，可能掉登录了（端口 %s）"
                           % (port or "自动发现"))
    _CK = ck
    try:
        json.dump({"ck": ck, "ts": time.time(), "port": port,
                   "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                  open(cf, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return ck


def csrf_token(port=None):
    """从 cookie 缓存里取 `csrf_token`。

    ★ 2026-11-03 第五轮破解：`POST /console/api/v1/*` **全部被一个全局中间件
      挡住**，不带 token 一律返回 `403 {"code":403,"msg":"invalid csrf token"}`
      —— 连瞎编的路径 `/console/api/v1/definitely-not-a-route` 也是 403，
      所以它不是路由级校验，是前缀级中间件。
      带上请求头 **`X-CSRF-Token: <csrf_token cookie 的值>`** 就通了（返回 404/数据）。

      试过无效的头名：`X-XSRF-TOKEN`、`x-tt-csrf-token`、`csrf-token`
      （这三个仍然 403）；放 body 里 `{"csrf_token":...}` 也无效。
    """
    try:
        ck = cookies(port)
    except Exception:
        return ""
    for seg in ck.split(";"):
        if "=" in seg:
            k, v = seg.strip().split("=", 1)
            if k.strip() == "csrf_token":
                return v.strip()
    return ""


def _hdr(port=None, referer=REFERER):
    h = {"Cookie": cookies(port), "User-Agent": UA, "Referer": referer,
         "Accept": "application/json, text/plain, */*"}
    t = csrf_token(port)
    if t:
        h["X-CSRF-Token"] = t
    return h


_S = None


def session():
    """共享连接池，**绕开系统代理**。

    为什么必须绕：本机装了系统代理（127.0.0.1:55642），并发 8~10 路时
    代理会把本地端口打光，报 `WinError 10048 每个套接字地址只允许使用一次`
    或 `ProxyError`。实测直连 music.douyin.com 200 / 0.32s，又快又稳，
    所以这里 trust_env=False + 显式关闭 proxies。
    """
    global _S
    if _S is None:
        import requests
        from requests.adapters import HTTPAdapter
        s = requests.Session()
        s.trust_env = False
        st = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
        s.mount("https://", st)
        s.mount("http://", st)
        _S = s
    return _S


def _req(method, path, params=None, body=None, port=None, referer=REFERER, timeout=40, tries=3):
    """带重试的请求（直连，不走代理）。"""
    s = session()
    last = None
    nop = {"http": None, "https": None}
    for i in range(tries):
        try:
            if method == "GET":
                r = s.get(HOST + path, params=params, headers=_hdr(port, referer),
                          timeout=timeout, proxies=nop)
            else:
                r = s.post(HOST + path, json=body if body is not None else {},
                           headers=_hdr(port, referer), timeout=timeout, proxies=nop)
            return _json(r)
        except Exception as e:          # 网络层异常才重试，业务错误码不重试
            last = e
            time.sleep(0.3 * (i + 1))
    raise last



def _get(path, params=None, port=None, referer=REFERER, timeout=40):
    return _req("GET", path, params=params, port=port, referer=referer, timeout=timeout)


def _post(path, body=None, port=None, referer=REFERER, timeout=40):
    return _req("POST", path, body=body, port=port, referer=referer, timeout=timeout)



def _json(r):
    try:
        return r.json()
    except Exception:
        return {"_status": r.status_code, "_text": (r.text or "")[:300]}


def _ok(j):
    return (j.get("baseResp") or {}).get("errorCode") == 0 or \
           (j.get("BaseResp") or {}).get("StatusCode") == 0


# ---------------- 发布额度 ----------------
def can_create(port=None):
    """每日发布/创作额度。返回 {can_create, limit, used, left}"""
    j = _get("/api/can-create", port=port)
    d = j.get("data") or {}
    lim = d.get("createDailyLimitation")
    used = d.get("currentSongCreateDailyCount")
    return {
        "ok": _ok(j),
        "can_create": d.get("canCreateSong"),
        "can_fragment": d.get("canCreateFragment"),
        "can_lyric": d.get("canCreateLyricCompose"),
        "limit": lim,
        "used": used,
        "left": (lim - used) if isinstance(lim, int) and isinstance(used, int) else None,
        "raw": d,
    }


# ---------------- 数据 ----------------
def trend(metric=2, scope=1, period=2, song_id=None, port=None):
    """按天趋势。metric 见 METRICS；scope=1 账号级、2 单曲级（要 song_id）；period=2 近7天、1 昨日

    返回 [{date, value, rate}]，rate 为环比（小数）
    """
    p = {"scope": scope, "period": period, "metricType": metric}
    if song_id:
        p["songID"] = song_id
    j = _get("/api/statistics/disclosure/trend", p, port=port)
    pts = ((j.get("data") or {}).get("points")) or []
    out = []
    for x in pts:
        try:
            v = float(x.get("totalValue"))
        except Exception:
            v = None
        try:
            rt = float(x.get("rate"))
        except Exception:
            rt = None
        out.append({"date": x.get("date"), "value": v, "rate": rt})
    return out


def home_page_info(port=None, **params):
    """首页汇总：收入、热度、抖音播放、作品数、热门视频…（响应约 133KB，已裁剪）

    ★★ 这个接口**收参数**，只是参数名很反直觉（2026-11-03 从 Lynx 模板
    `GetHomePageInfo` 源码里抠出来的，全部五个）：
        cashIncomeDate / trafficRevenueDate / trafficRevenueSongId
        dataSummaryStartTime / dataSummaryEndTime

    `trafficRevenueSongId` 就是**单曲流量饼图**的开关 —— 之前一直以为
    「home-page-info 忽略所有入参」，是因为传的是 songId（错名），
    服务端静默丢弃才表现得像无参。传对名字立刻生效：见 traffic_pie()。

    注意 `dataSummary*` 改了只影响 dataSummary 块，`trafficRevenueBrief`
    和它互不干扰；反过来 trafficRevenueSongId 也只动 composition。
    """
    j = _post("/api/mini-app/home-page-info", params or {}, port=port)
    d = j.get("data") or {}
    brief = d.get("dataSummaryBrief") or {}
    summ = brief.get("dataSummary") or {}
    bonus = brief.get("signingBonus") or {}
    cash = d.get("cashIncomeBrief") or {}
    hot = ((d.get("hotVideoList") or {}).get("items")) or []
    return {
        "cash_income": summ.get("cashIncome"),
        "luna_popularity": summ.get("lunaPopularity"),
        "aweme_vv": summ.get("awemeVV"),
        "aweme_usage": summ.get("awemeUsage"),
        "signing_bonus_income": bonus.get("cashIncome"),
        "signing_bonus_popularity": bonus.get("lunaPopularity"),
        "full_song_count": brief.get("fullSongCount"),
        "clip_count": brief.get("clipCount"),
        "royalty_income": cash.get("royaltyIncome"),
        "hot_videos": len(hot),
        "raw_keys": sorted(d.keys()),
    }


# ★★ 流量饼图的两个扇区（2026-11-03 逐首验证）
PIE_CATEGORY = {
    1: "冷启动",        # value = 签约权益下发的曝光配额（整数、2万起、红心可叠加）
    3: "打歌视频",      # value ≈ 抖音视频播放量（cvvvAll，快照时间差几十到几百）
}
PIE_TYPE = {1: "曝光", 2: "热度"}


def traffic_pie(song_id=None, month=None, port=None):
    """★★ 流量收益饼图 —— 单曲版 / 账号版，一次拿全。

    走的是 `POST /api/mini-app/home-page-info`，靠 `trafficRevenueSongId` 切到单曲。
    **这是全站唯一能拿到「冷启动曝光」的地方** —— 统计接口族
    (consoleOverview / singleOverview / index・overview / songDetails /
     weekly・report / trend mt1-8 / singleTimeseries / ugc-statistics)
    九个全部翻过，一个曝光字段都没有。原因：曝光是**签约权益口径**，
    不是统计口径，所以只出现在小程序首页这个「权益展示位」。

    返回 items 语义（category 见 PIE_CATEGORY，type 见 PIE_TYPE）：

      cat=1 type=2  冷启动曝光配额。实测恒为整数 20000 = 官方权益表
                    「独家签约·歌曲上线曝光 2万次」；红心 100/500/1000
                    按权益表应追加 2万/5万/10万，故可能出现 4万/7万/12万。
                    账号级 33701948 = signingBonus.lunaPopularity(16850974) × 2，
                    双倍热度计入，**不是**逐首配额之和，别拿它除首数。
      cat=3 type=1  打歌视频带来的曝光量，≈ 该曲 douyinVideoPlays(cvvvAll)。
                    实测 26925 vs 26989，差值是快照时间差；cvvvAll=0 时
                    这一项**不出现**（不是 0，是缺项）。

    新发歌曲返回 `items: []`（权益还没生效），不是接口坏了。
    """
    p = {"trafficRevenueSongId": song_id} if song_id else {}
    if month:
        p["trafficRevenueDate"] = _month_ts(month)
    j = _post("/api/mini-app/home-page-info", p, port=port)
    d = j.get("data") or {}
    tr = d.get("trafficRevenueBrief") or {}
    items = ((tr.get("composition") or {}).get("items")) or []
    out = []
    for it in items:
        try:
            v = int(it.get("value"))
        except (TypeError, ValueError):
            v = it.get("value")
        out.append({
            "category": it.get("category"),
            "type": it.get("type"),
            "value": v,
            "label": PIE_CATEGORY.get(it.get("category"), it.get("category")),
            "metric": PIE_TYPE.get(it.get("type"), it.get("type")),
        })
    total = sum(x["value"] for x in out if isinstance(x["value"], int))
    return {
        "ok": _ok(j),
        "song_id": song_id,
        "show": tr.get("showTrafficRevenue"),
        "items": out,
        "total": total,
        # 顺手带出能交叉验证的口径
        "account": {
            "luna_popularity": (d.get("dataSummaryBrief", {}).get("dataSummary") or {}).get("lunaPopularity"),
            "aweme_vv": (d.get("dataSummaryBrief", {}).get("dataSummary") or {}).get("awemeVV"),
            "signing_bonus_popularity": (d.get("dataSummaryBrief", {}).get("signingBonus") or {}).get("lunaPopularity"),
        },
    }


def home_preview(port=None):
    """首页 4 张数据卡（响应仅 141 字节，全站最轻的数据接口）。

    **周期参数无效**：period / days / scope 怎么传都返回同一份，别再试 ?period=1/7/30。
    字段是字符串（"4101"），不是数字。

    口径（2026-11-03 用 trend 逐 metric 反推验证过）：
      luna_vv / cv_7d / cvvv_7d 三个字段**精确等于** trend 的近 7 日合计
        = mt2 汽水播放量 / mt5 抖音投稿量 / mt7 抖音视频播放量（分毫不差）
      luna_popularity / douyin_popularity 两个指数**对不上** trend 的 mt1/mt6：
        既不等于 7 日合计，也不等于其中任何单日。两者之比 12.42 与
        mt6/mt1 的 7 日合计比 12.40 几乎一致，最可能是「今日实时」口径
        （trend 最新只到昨天，今天的数进不去）—— **未验证，别当 7 日合计用**。
    """
    j = _get("/api/statistics/disclosure/home-preview", port=port)
    d = j.get("data") or {}

    def num(k):
        v = d.get(k)
        try:
            return int(v)
        except (TypeError, ValueError):
            return v

    return {
        "ok": _ok(j),
        "luna_popularity": num("lunaPopularity"),   # 汽水热度指数（口径未验证）
        "luna_vv": num("lunavv"),                   # 汽水播放量 近7日 = mt2 合计 ✅
        "cv_7d": num("cv7Days"),                    # 抖音投稿量 近7日 = mt5 合计 ✅
        "cvvv_7d": num("cvvv7Days"),                # 抖音视频播放量 近7日 = mt7 合计 ✅
        "douyin_popularity": num("douyinPopularity"),  # 抖音热度指数（口径未验证，卡片不显示）
        "raw": d,
    }


def hot_content(port=None):
    """站内热歌 / 热歌手 / 热门视频榜。

    ⚠️ 前端 chunk 里内置了一份**写死的兜底榜单**（打火机 / Penny 等），
    接口失败时会静默渲染假数据。调用方必须判 ok，别把可疑 payload 当真实榜单。
    数量字段是格式化字符串（"600w+"），不能直接参与计算。
    """
    j = _get("/api/home/hot-content", port=port)
    d = j.get("data") or {}
    return {
        "ok": _ok(j),
        "hot_songs": d.get("hotSongs") or [],
        "hot_artists": d.get("hotArtists") or [],
        "hot_videos": d.get("hotVideos") or [],
        "raw_keys": sorted(d.keys()),
    }


def _month_ts(month):
    """把 "202610" / "2026-10" / "20261015" 转成该月 1 号 00:00 的秒级时间戳。

    `/api/mini-app/traffic-revenue` 的 `date` 参数是「月份锚点」，
    月内任意一天等价，这里统一取 1 号，保证可缓存、可复现。
    """
    s = str(month).replace("-", "").replace("/", "").strip()
    if len(s) < 6:
        raise ValueError("month 至少要到月：202610 / 2026-10")
    y, m = int(s[:4]), int(s[4:6])
    return int(datetime.datetime(y, m, 1).timestamp())


def traffic_revenue(song_id=None, month=None, port=None):
    """汽水收藏数 / 评论数 / 抖音涨粉 / 抖音视频播放与使用数。

    **这是网页端完全没有的字段**（控制台的歌曲表不提供收藏数、评论数）。
    来历：手机端「抖音小程序·音乐人数据中心」能看到，小程序接口在
    `/api/mini-app/` 前缀下，带同一份 cookie 就能直连。

    带 song_id 时返回单曲维度；**不带则返回账号汇总**（实测 2026-11-03）：
      lunaCollections 24188 / lunaComments 9680 / douyinNewFollowers 1484
      douyinVideoPlays 2843850 / douyinVideoUses 3491

    ★ `song_id=-1` 等价于「不带」= 手机端下拉里的「全部歌曲」项。

    ★ `month`（新发现，2026-11-03 二次测绘）：底层参数名是 **`date`**，
      但**不是日期，是月份锚点**——传该月内任意一天的时间戳（秒）都得到同一个数。
      实测「不婚律令」：10-22 与 10-31 都是 220，11-01 与 11-02 都是 5，
      而不加 date = 225 = 220 + 5 → **逐月可加总回累计**。
      `date` 绑定成整数：传 ""/"2026-10"/"true" 报
      「parameter type does not match binding data」；传 0 或随机数返回 `{}`。
      ⚠ 数据只覆盖 **202610 起**（202604~202609 逐月全空，但账号累计是从 4 月起的，
        且用户 7/8/9 月截图实证该模块当时已展示按月冷启数据）→ 不是「模块 10 月才上线」，
        而是**后端滚动清理旧月**：现在只能拉到 Oct/Nov，7–9 月已从 API 清除（仅存用户截图）。
        所以别拿它当全历史，历史按月冷启明细无法经客户端还原。
      ⚠ `cash_income_detail` 的同名参数要的是 **YYYYMM 字符串**，两个接口不一致。

    ★ 三条实测坑（不按这个读会得出完全错误的结论）：
      1. **缺失的 key 就是 0，不是「无数据」**。播放量小的歌只回
         `{"douyinNewFollowers": 1484}`，此时收藏/评论/视频播放全是 0。
      2. **`douyinNewFollowers` 是账号级**，带了 songId 也恒返回 1484，
         同一首歌和别的歌一模一样 —— 不能当单曲涨粉用。
         （但带 `date` 时它会变成**当日/当月增量**，实测出现过 -1。）
      3. 非法 songId（"0"/"111"/"999999999"）返回 `data.detail = {}` 且
         `ok=True`，**没有报错**，只能靠「字段全缺」区分。
         songId 只认汽水 track_id：抖音曲 id(externalInfo.id)、albumId、clipId 全空。
    """
    p = {}
    if song_id is not None:
        p["songId"] = song_id
    if month:
        p["date"] = _month_ts(month)
    j = _get("/api/mini-app/traffic-revenue", p or None, port=port)
    d = ((j.get("data") or {}).get("detail")) or {}

    def num(k):
        v = d.get(k)
        try:
            return int(v)
        except (TypeError, ValueError):
            return v

    return {
        "ok": _ok(j),
        "luna_collections": num("lunaCollections"),
        "luna_comments": num("lunaComments"),
        "douyin_new_followers": num("douyinNewFollowers"),
        "douyin_video_plays": num("douyinVideoPlays"),
        "douyin_video_uses": num("douyinVideoUses"),
        "raw": d,
    }


def traffic_account(port=None):
    """账号级互动汇总（不给 songId 的那条路）。返回 5 个整数。"""
    j = _get("/api/mini-app/traffic-revenue", None, port=port)
    d = ((j.get("data") or {}).get("detail")) or {}
    out = {}
    for k in ("lunaCollections", "lunaComments", "douyinNewFollowers",
              "douyinVideoPlays", "douyinVideoUses"):
        try:
            out[k] = int(d.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    return out


def traffic_composition(port=None):
    """★ 流量来源饼图 —— 冷启动数据的**唯一**来源（账号级，无单曲维度）

    手机端「数据中心 → 更多 → 流量收益」顶部那张环形图就是这个。
    数据藏在首页大包 `home-page-info` 的 `trafficRevenueBrief.composition` 里，
    **没有单独接口**（`/api/mini-app/` 命名空间实测只有 4 个接口，见
    probe_miniapp_all.py）。

    2026-11-03 实测：
        {"items":[{"category":1,"type":2,"value":"33701948"},
                  {"category":3,"type":1,"value":"2627412"}]}
    合计 36,329,360。

    ★ 哪个是冷启动（2026-11-03 实测锁定，三条独立证据）：
      ① **33,701,948 = 16,850,974 × 2，严丝合缝两倍**。
         16,850,974 正是 `dataSummaryBrief.signingBonus.lunaPopularity`
         （签约激励带来的汽水热度）—— 独家签约按双倍热度计入，冷启动曝光翻倍。
         乘 2 能对上整数倍，不可能是巧合。
      ② 2,627,412 ≈ 0.93 × awemeVV(2,820,674)，量级与抖音侧
         douyinVideoPlays(2.84M) 同档 → 是抖音侧的「入驻打歌视频曝光」。
      ③ 汽水累计播放 4,286,735 / 33,701,948 = **12.72%** 曝光→播放，音乐流合理。

      → **33,701,948 = 签约汽水冷启动流量（92.77%）**
         **2,627,412  = 入驻打歌视频曝光（7.23%）**

    ⚠ 别用「曝光按首均摊 × 转化率」去验（我试过，会得出错误结论）：
      33.7M 是**全账号 2025-08 至今**的累计量，包含大量早期老歌，
      摊到 772 首冷启动歌上会算出每首 5,553 播放，而实测均值只有 1,308。

    ⚠ 该数据**不受参数影响**：传 songId / period / 日期区间全部返回同一份
      （home-page-info 只认 POST、且无任何入参），所以拿不到单曲、拿不到周期。

    ★★ 2026-11-03 二次穷尽（手机端确实能按单曲显示饼图，但接口层拿不到）：
      已排除的 6 条路，全部为负，不要再重复试：
        · home-page-info 传 5 种 body（songId / -1 / id / song_id …）→ composition 逐字节不变
          （比对时只比这块，避开 hotVideoList 封面 `l=` 随机串）；
        · /api/statistics/disclosure/ 下 624 个「流量来源/曝光/冷启」类路径 → **0 命中**；
        · /api/mini-app/ 补扫 POST 114 条 → 0 命中（此前 4,969 条全组合是 GET）；
        · traffic-revenue 穷举 55 个参数名 × 7 值 = 385 次 → 只多认一个 `date`；
        · 换 host 不换路径：musician.douyin.com 同源同数据，其余 5 个 host 404；
        · 前端 bundle 硬证据：`main.js` 里小程序客户端**只有 4 个方法**
          （GetHomePageInfo / GetCashIncomeDetail / GetTrafficRevenue / GetUserSubscription），
          「汽水音乐合作平台」musician.douyin.com 的 chunk 也只用 `/console/api/v1`，无新增。

      → 结论：**单曲饼图是小程序端自己画的**。两个旁证：
        ① 「不婚律令」饼图蓝扇区 224 ≈ 该歌 `douyinVideoPlays` 225（截图时点差 1），
           即蓝扇区 = 抖音视频播放累计，这个我们有；
        ② 「奶茶醒神令」绿扇区「2万」顺着 33,701,948 = 2 × 签约热度 推：
           单曲冷启 ≈ **2 × 该歌的签约热度**。而「签约热度」**没有单曲接口**
           （账号 16,850,974 vs 自然热度 2,314,575，全账号 7.28 倍，单曲摊不到）。
           ⚠ 别用「33,701,948 ÷ 20,000 = 1,685 首」去推「每首固定 2 万」：
             我试过，1,685 对不上任何可观测歌数（播放≥20 的有 2,748 首、
             播放≥1 的有 3,730 首），这条推断不成立，已废弃。
           → 所以单曲冷启**量**确实拿不到；能拿到的只有「有/无」，
             靠 cold_start.py 的曲线形态判，那才是现有唯一路子。
    """
    j = _post("/api/mini-app/home-page-info", {}, port=port)
    d = (j.get("data") or {}).get("trafficRevenueBrief") or {}
    items = ((d.get("composition") or {}).get("items")) or []
    out = {"ok": _ok(j), "show": d.get("showTrafficRevenue"),
           "cold_start": 0, "promote_video": 0, "total": 0, "items": items}
    for it in items:
        try:
            v = int(it.get("value") or 0)
        except (TypeError, ValueError):
            v = 0
        # type=2 & category=1 → 签约汽水冷启动流量；type=1 & category=3 → 入驻打歌视频曝光
        if it.get("type") == 2:
            out["cold_start"] += v
        else:
            out["promote_video"] += v
        out["total"] += v
    if out["total"]:
        out["cold_start_pct"] = round(100.0 * out["cold_start"] / out["total"], 2)
        out["promote_pct"] = round(100.0 * out["promote_video"] / out["total"], 2)
    return out


def cash_income_detail(date, port=None):
    """现金收益明细（小程序）：版税收益 / 签约专属激励 的结算周期趋势。

    `date` 必填，格式 **YYYYMM**（`2026-11` 会报 -1004 RPC 错误，`202611` 才对）。
    缺参时报错会直接给出参数名：
        「参数错误: binding: expr_path=date, cause=missing required parameter」
    —— 这个报错格式可以当「参数名探测器」用。

    返回 royaltyIncome / royaltyIncomeTrend / signingBonusTrend / earliestDate，
    Points 为 [{XAxis 结算周期, YAxis 金额}]。本账号 Points 目前为空。
    """
    j = _get("/api/mini-app/cash-income-detail", {"date": str(date)}, port=port)
    d = j.get("data") or {}
    return {"ok": _ok(j), "royalty_income": d.get("royaltyIncome"),
            "royalty_trend": d.get("royaltyIncomeTrend"),
            "signing_bonus_trend": d.get("signingBonusTrend"),
            "earliest_date": d.get("earliestDate"),
            "latest_date": d.get("latestDate"), "raw": d}


def user_subscription(port=None):
    """小程序消息订阅状态。`[{TemplateID, Status}]`，Status=2 表示已订阅/已同意。"""
    j = _get("/api/mini-app/user-subscription", None, port=port)
    return {"ok": _ok(j),
            "subscriptions": (j.get("data") or {}).get("subscriptions") or []}


def song_info(song_id, port=None):
    """单曲完整信息（2026-11-03 新挖）—— **比 `song/list/v2` 多一倍字段**。

    `GET /console/api/v1/song/info?id=<songId>`   ← 参数名是 **id**，不是 songId
    传 songId/musicId/trackId 都会报 `50005 get check params error!`。

    比 song/list 多出来的关键字段：
        publishTime / editability / displayStatusV2 / rejectTimes /
        rejectReasonV2 / publishRangeType / songContractId / contractId /
        packageTemplate / hasSupplementaryPackage / supplementaryPackageList /
        isPlayerByCreator / isProducerByCreator / songAuthorizationType /
        multiPartyUserIDs / proveFileUrls / clips[].clipUrl（音频直链）
    另外 `album` 子对象带 `memberId`（作品库 id）、`productArtists[].artist`
    带 `metaArtistId` / `awemeId`。
    """
    j = _get("/console/api/v1/song/info", {"id": song_id}, port=port)
    return {"ok": (j.get("statusCode") == 0), "data": j.get("data") or {},
            "msg": j.get("errMsg")}


def song_data(mid, start, end, port=None):
    """单曲区间数据 —— ⚠ **参数语义未破解**。

    `GET /console/api/v1/song/data?mid=&start=&end=`
    - 缺 mid → `50005 mid could not be null`
    - 有 mid 缺日期 → `50005 check date range but start or end was empty!`
    - 三个都齐 → `statusCode=0, data=[]`

    问题：`mid` 试过 songId / albumId / memberId / userId / clips[].clipId，
    **全部返回空数组**；连 `mid=abc` 也是空数组，说明「找不到」和「无数据」
    是同一个返回，没法用错误信息反推。日期也试了 unix 秒/毫秒、
    YYYYMMDD、YYYY-MM-DD 四种，都通（不报错）但都空。
    → 接口确实存在，但 `mid` 是哪个体系的 id 仍未确认，暂不接入业务。
    """
    j = _get("/console/api/v1/song/data",
             {"mid": mid, "start": start, "end": end}, port=port)
    return {"ok": (j.get("statusCode") == 0), "data": j.get("data"),
            "msg": j.get("errMsg")}


def albums_list(port=None):
    """全部专辑（免签名、无参数）。返回 `{"albums":[...]}`。"""
    j = _get("/console/api/v1/albums/list", port=port)
    return {"ok": bool(j.get("albums") is not None), "albums": j.get("albums") or []}


def album_detail(album_id, port=None):
    """专辑详情。`GET /console/api/v1/albums/detail?albumId=`（缺参报
    「属性 albumId: 缺少数据」）。"""
    j = _get("/console/api/v1/albums/detail", {"albumId": album_id}, port=port)
    return {"ok": bool(j.get("album")), "album": j.get("album") or {}}


def label_info(port=None):
    """账号身份：`{"label":{"memberId","userId","status","role"}}`。

    本账号 role=2、status=0、memberId 为空 —— 说明这是**个人音乐人**，
    没有机构/厂牌身份。这也解释了为什么「歌曲洞察」(`/promotion/all/data`)
    提示「暂未向个人音乐人开放」。
    """
    j = _get("/console/api/v1/label/info", port=port)
    return j.get("label") or {}


def settlement_overview(port=None):
    """收入概览。金额单位是 **0.01 元**（243601 → 2436.01 元）"""
    j = _get("/console/api/v1/settlement/overview/v2", port=port)
    d = j.get("data") or j
    def money(k):
        v = d.get(k)
        try:
            return round(int(v) / 100.0, 2)
        except Exception:
            return v
    return {
        "cumulative_income": money("cumulativeIncome"),
        "withdrawable": money("withdrawableBalance"),
        "withdrawn": money("withdrawnAmount"),
        "last_period_income": money("lastPeriodIncome"),
        "last_period_time": d.get("lastPeriodTime"),
        "disable_withdraw": d.get("disableWithdraw"),
        "raw": d,
    }


# ---------------- 资产 / 作品 ----------------
def work_list(page=1, size=200, user_work_type=2, port=None):
    """创作台资产列表。**PageSize 实测可到 1000**（返回数约为请求数的 96.7%）"""
    j = _get("/studio_api/assets/work-list",
             {"CurrentPage": page, "PageSize": size, "UserWorkType": user_work_type}, port=port)
    d = j.get("data") or {}
    # Total 有时候是字符串（"5689"），统一转 int，否则下游算页数会 TypeError
    try:
        total = int(d.get("Total") or 0)
    except (TypeError, ValueError):
        total = 0
    return {"works": d.get("UserWorks") or [], "total": total, "ok": _ok(j)}


def iter_works(size=1000, user_work_type=2, port=None, verbose=True):
    """全量迭代。5689 首 ≈ 6 次请求"""
    first = work_list(1, size, user_work_type, port)
    total = first["total"] or 0
    yield from first["works"]
    got = len(first["works"])
    pages = max(1, (total + size - 1) // size) if total else 1
    if verbose and pages > 1:
        print("作品 %d 首 / %d 页（每页 %d）" % (total, pages, size))
    for p in range(2, pages + 1):
        r = work_list(p, size, user_work_type, port)
        yield from r["works"]
        got += len(r["works"])
        if verbose:
            print("  %d/%d  %d 首" % (p, pages, got))


def song_list(page=1, size=20, audio_type=2, port=None):
    """已上架歌曲列表。songAudioType: 1=纯音乐/伴奏（我们为空）2=歌曲"""
    j = _get("/console/api/v1/song/list/v2",
             {"currentPage": page, "pageSize": size, "songAudioType": audio_type},
             port=port, referer="https://music.douyin.com/console/songs")
    return j.get("data") or {}


def albums(page=1, size=50, port=None):
    j = _get("/console/api/v1/albums/list", {"currentPage": page, "pageSize": size},
             port=port, referer="https://music.douyin.com/console/songs")
    return j.get("albums") or []


def draft_search(dtype=6, keyword="", port=None):
    """草稿搜索 type=6/7（实测无草稿，返回空 list）"""
    j = _get("/console/api/v1/draft/search", {"type": dtype, "keyword": keyword},
             port=port, referer="https://music.douyin.com/console/create")
    return j.get("list") or []


def musician_profile(port=None):
    j = _get("/console/api/v1/musician/profile", port=port)
    return j.get("data") or j


# ---------------- CLI ----------------
def main():
    a = sys.argv[1:]
    cmd = a[0] if a else "quota"
    if cmd == "quota":
        q = can_create()
        print("今日额度：上限 %s，已用 %s，剩 %s（可发：%s）"
              % (q["limit"], q["used"], q["left"], q["can_create"]))
    elif cmd == "trend":
        m = int(a[1]) if len(a) > 1 else 2
        for pt in trend(m):
            print("  %s  %10s  %+6.1f%%" % (pt["date"], pt["value"], (pt["rate"] or 0) * 100))
    elif cmd == "home":
        print(json.dumps(home_page_info(), ensure_ascii=False, indent=1))
    elif cmd == "preview":
        print(json.dumps(home_preview(), ensure_ascii=False, indent=1))
    elif cmd == "hot":
        h = hot_content()
        print("ok=%s  热歌 %d / 热歌手 %d / 热门视频 %d"
              % (h["ok"], len(h["hot_songs"]), len(h["hot_artists"]), len(h["hot_videos"])))
        for s in h["hot_songs"][:10]:
            print("  ", json.dumps(s, ensure_ascii=False)[:120])
    elif cmd == "settle":
        print(json.dumps(settlement_overview(), ensure_ascii=False, indent=1))
    elif cmd == "works":
        n = int(a[1]) if len(a) > 1 else 5
        for i, w in enumerate(iter_works(size=200, verbose=False)):
            if i >= n:
                break
            ii = w.get("IssueInfo") or {}
            ct = w.get("CreateTime")
            try:
                ts = datetime.datetime.fromtimestamp(int(ct)).strftime("%Y-%m-%d")
            except Exception:
                ts = str(ct or "")[:10]
            print("  %-22s %s  %s  %s" % (
                (w.get("Title") or "")[:22],
                "已发" if ii.get("Issued") else "未发",
                ts, str(ii.get("IssueAddress") or "")[-32:]))
    elif cmd == "quota_all":
        for m, name in METRICS.items():
            pts = trend(m)
            tot = sum(p["value"] or 0 for p in pts)
            print("  %-12s 7日合计 %10.0f" % (name, tot))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()


def console_overview(port=None):
    """账号总览（2026-11-03 第七轮新发现，比 home-preview 全）。

    GET /api/statistics/consoleOverview  —— 不用 /console/api/v1 前缀，也不需要参数。
    字段：cvAll(投稿总) / cv7Days / cvvvAll(抖音视频播放总) / cvvv7Days /
          lunaPopularity(汽水热度) / lunaFavoriteAll(汽水收藏总) / lunaFavorite7Days /
          fansAll(涨粉) / liveKtvAll(直播K歌) / distributionSaleNumAll
    ⚠ 依然**没有任何曝光字段** —— 冷启曝光在统计接口族里根本不存在。
    """
    j = _get("/api/statistics/consoleOverview", None, port=port)
    return {"ok": _ok(j), "raw": j.get("data") or {}}


def song_overview(song_id, port=None):
    """单曲总览。参数是 **小写 songId**（和大写 songID 的那批不一样）。

    GET /api/statistics/singleOverview?songId=
    字段：cvvvAll / cvAll / liveKtvAll / lunaFavoriteAll / lunaPopularity / authType / includeFullClip
    """
    j = _get("/api/statistics/singleOverview", {"songId": song_id}, port=port)
    return {"ok": _ok(j), "raw": j.get("data") or {}}


_ORIGIN_LABEL = {
    "edit_page_recommend": "音乐选择页系统推荐",
    "edit_page_favourite_luna_favorite": "剪辑页·汽水红心",
    "edit_page_favourite_tiktok_favorite": "剪辑页·抖音红心",
}


def origin_distribution(song_id, port=None):
    """单曲「发布来源分布」——**不是**「流量收益」那张冷启/抖音饼图，别混。

    GET /console/api/v1/statistics/publishOriginDistribution?songId=
    返回 [{"category": i18n_key, "percentage": 16.7}, ...]
    语义 = 发视频时从哪个入口选的这首歌。
    ⚠ 百分比全部量化到 1/6（16.7/33.3/50/66.7/100），像只取 6 个桶，疑似半成品；
      且只有百分比，没有绝对值。
    """
    j = _get("/console/api/v1/statistics/publishOriginDistribution",
             {"songId": song_id}, port=port)
    items = j.get("data") or []
    for it in items:
        it["label"] = _ORIGIN_LABEL.get(it.get("category"), it.get("category"))
    return {"ok": _ok(j), "items": items}

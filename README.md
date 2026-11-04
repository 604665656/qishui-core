# qishui-core

汽水音乐（抖音创作者平台）工具箱的**公共内核**。无界面、无重依赖，被宿主和每个插件共同依赖。

它不是 SDK，也不是爬虫框架——它只解决这几个具体问题：

| 模块 | 解决什么 |
|---|---|
| `paths` | **数据到底存哪**。所有工具共用一套路径约定，绝不写"当前工作目录" |
| `auth` | **登录态从哪来**。走 CDP 从已开的浏览器取 Cookie，不碰密码、不存密码 |
| `store` | **多账号怎么不打架**。`accounts.json` 注册表 + 当前账号 + 端口分配 |
| `launcher` | 用独立 profile 拉起带 CDP 的浏览器（多账号的物理基础） |
| `api` | 创作者后台 40+ 接口的直连封装（纯 `requests`，不需要签名） |
| `updater` | 历史版本归档 + 热更新（清单→下载→校验→暂存→换目录） |
| `schedule` | 每日定时：进程内定时器 + Windows 计划任务（带 `.bat` 兜底） |
| `context` | `ToolContext`：插件拿到的唯一通行证（含 `ctx.accounts` 账号管理） |
| `standalone` | 让插件"不靠宿主也能跑"的那几十行 |

## 安装

```bash
pip install -e .
# 或从 GitHub
pip install git+https://github.com/604665656/qishui-core.git
```

只要 `requests`。没有 PySide6、没有 pandas、没有 numpy。

## 30 秒上手

```python
from qishui_core import api, auth, store

# 1) 登录态：浏览器要用 --remote-debugging-port 启动
ck = auth.cookie()                  # 自动找端口；缓存 6 小时
print(auth.has_login(ck))           # True
print(auth.uid_from(ck))            # uid_tt

# 2) 多账号：每个账号绑一个调试端口
uid, acct_dir, port = store.resolve()
api.use_port(port)                  # 之后所有调用都用这个号

# 3) 调接口
print(api.can_create())                     # 今日还能发几首
print(api.trend(metric=2, period=2))        # 汽水播放量 7 日趋势
print(api.traffic_pie(song_id="74xxxx"))    # 单曲流量饼图（含冷启动 cat=1）
```

## 账号管理：`ctx.accounts`（`api_version >= 2`）

插件**不许**直接读写 `accounts.json`。账号表里放着端口、profile 路径、当前账号，
随便改就会出三件事：别的插件的账号被改掉、两个账号抢同一个 profile 互相顶登录、
以及"新槽位 uid 空着却显示成别人的曲库"这种最难查的串号。所以只开一扇带语义的门：

```python
A = ctx.accounts
A.list(True)              # 全部条目（含「分配了端口但还没登录」的槽位）
A.real()                  # 已登录的账号，按端口升序（主号在前）
A.register("新账号")      # 建槽位：自动分配端口 + 独立 profile
A.bind(port, uid)         # 登录成功后回填 uid
A.set_uid(old, new)       # 按实际登录的账号重写登记（校正 uid）
A.remove(port=..)         # 移除登记（待登录槽位 uid 空，只能按端口认）
A.set_current(uid)        # 切当前账号，宿主会广播给所有插件
A.stats(uid)              # {total, plays, date, has_data}
A.probe(port)             # {online, login, uid} 活体实况
A.launch(port, profile)   # 拉起带 CDP 的浏览器（窗口默认开在屏幕外）
A.clear_login(port)       # 退出登录：清空**全部** Cookie 并回创作者后台
```

`set_current()` 必须走宿主：只改注册表不改各插件的 ctx，抓取会静默抓到上一个账号
的数据。这类串号查起来极费劲，所以内核里把它做成一条路。

**`stats("")` 必须返回空**，不能因为 `uid` 空就回退到"当前账号"的目录——
否则新加的槽位会显示成别人的曲库。

**`uid_tt` 每次重新登录都会变。** 拿它比对登记 uid 会把同一个号误报成
「被其它账号占用」。判定"这个槽位里是谁"时，只有两边都有值才比，比不上就别报警。

## 数据放哪：一个环境变量说了算

```
优先级：QISHUI_HOME 环境变量 > 打包版 exe 所在目录 > 源码仓库根
```

```
<数据根>/
  accounts.json              # 账号注册表
  accounts/<uid>/            # 每个账号的数据（换号不串）
  accounts/_current.txt      # 当前账号
  plugin_data/<pid>/         # 插件私有数据（跨账号配置、日志、版本归档）
  plugins/                   # 装好的插件
  _cookie_cache_<port>.json  # Cookie 缓存（按端口分文件，多账号不互相覆盖）
  _schedule.json             # 定时任务登记
```

想开一套干净的沙盒跑测试：

```bash
QISHUI_HOME=/tmp/qishui-sandbox python your_script.py
```

## 几个踩过的坑（都已在代码里处理）

**Cookie 缓存必须按端口分文件。** 早期所有账号共用一个 `_cookie_cache.json`，
换号后还在用上一个号的 Cookie，表现是"数据看起来对但其实是另一个号的"。现在
`_cookie_cache_9222.json` / `_cookie_cache_9223.json` 各存各的，`api.use_port()`
切换到哪个端口就用哪个文件。

**服务端只保留最近两个月的聚合。** 平台的月度聚合接口会滚动清理，几个月前的
`detail` 直接变成 `{}`。所以**该记的东西必须自己先记下来**——这正是"每日快照"
存在的理由。`updater` 的历史版本目录也因此放在 `plugin_data/` 而不是插件目录里：
换目录、卸载都会整目录搬走，放里面等于没备份。

**`schtasks` 不一定让你调。** 组策略、安全软件、沙盒都可能拦。`schedule`
第一次探到不可用就记住，后面直接生成一个双击即可安装的 `启用…bat`，把
"程序自己注册"降级成"用户点一下"。

## 热更新怎么工作

```
update.json（清单，可放 GitHub raw）
  └─ releases[]: {version, url, sha256, notes}
       ↓ check()  比对版本
       ↓ stage()  下载 → 校验 sha256 → 解到 _staged/<ver>/
       ↓ 宿主重启前 apply_staged()
            生成 _swap_<id>_<ts>.py → 等进程退出 → 整目录替换 → 拉起新进程
```

Windows 上运行中的 `exe` / `pyd` / `dll` 换不掉，所以"下载"和"生效"必须分开。
`updater` 把这件事做成两步，并且替换失败会自动把旧目录改回来。

历史版本由 `snapshot()` 在每次替换前自动归档，`rollback()` 一键回去，默认保留 4 份。

```bash
python -m qishui_core.updater list     --id coldstart
python -m qishui_core.updater check    --id coldstart --feed https://…/update.json
python -m qishui_core.updater apply    --id coldstart --pid 12345
python -m qishui_core.updater rollback --id coldstart --ver 0.9.0
```

## 每日定时

```python
from qishui_core.schedule import DailyScheduler

s = DailyScheduler().start()
s.add("report", run_report, "07:30", owner="myplugin")
print(s.tasks())        # {last_run, last_at, last_err, last_sec}
```

同一任务可能在"进程内定时器"和"系统计划任务"两处同时到点，靠 `last_run`
按天去重，谁先到谁算。

## 许可

MIT。数据是你自己的，代码随便用；但请遵守平台的服务条款，别拿它做刷量的事。

# 飞书日程接入（CalDAV，一次性设置）

daily-report 通过 **CalDAV** 读你的飞书日程——和你在 Apple 日历里配的那个 CalDAV 账号
完全一样（服务器地址 + 用户名 + 飞书生成的 CalDAV 专用密码）。不需要任何开放平台应用、
管理员权限或 OAuth。取数脚本 `gather_calendar.py` 会按当天本地时区查询，重复性日程由服务端展开。

> 为什么不用飞书开放平台 app：openclaw 里那个自建 app（`cli_a92b1149ac78dcb2`）不是你管理的，
> 控制台「无权访问」，改不了它的权限。CalDAV 用你个人账号，绕开这个问题。

## 三项凭证从哪拿

**从飞书里：** 日历设置 → 「通过 CalDAV / 其他客户端访问」→ 里面给出**服务器地址**、**账号**，
并可**生成 CalDAV 专用密码**（不是你的登录密码）。

**或从 Mac 里：** 系统设置 → 互联网账户 → 选中那个飞书/CalDAV 账号 → 可看到**服务器**和**用户名**；
密码在钥匙串里，看不到就回飞书重新生成一个。

需要三样：

1. **服务器地址 URL**（如 `https://caldav.feishu.cn/`，以你看到的为准）
2. **用户名**（一般是你的飞书邮箱/账号）
3. **CalDAV 专用密码**（在飞书生成的那串）

## 存进来

```bash
python3 ~/my/daily_report/gather_calendar.py --set-caldav
# 然后按提示粘 3 行：第1行服务器URL、第2行用户名、第3行密码（从 stdin 读，不进命令行历史）
```

存到 `~/my/daily_report/.feishu_caldav.json`（权限 600）。
也可改用环境变量：`FEISHU_CALDAV_URL` / `FEISHU_CALDAV_USER` / `FEISHU_CALDAV_PASSWORD`。

## 验证

```bash
python3 ~/my/daily_report/gather_calendar.py --probe        # 连上并列出日历
python3 ~/my/daily_report/gather_calendar.py 2026-06-01     # 打印当天日程
```

## 依赖

```bash
pip install requests icalendar recurring_ical_events
```

（已装在当前 `python3` 环境：anaconda3 / icalendar 7.1.2 / recurring_ical_events。）

## 备注

- 默认会遍历该账号下**所有日历**并合并；跨日日程按当天窗口裁剪显示时间；CANCELLED 的过滤掉；按 (标题,开始时间) 去重。
- 密码失效（飞书里重置了）就重新 `--set-caldav` 覆盖一次。
- 国内站服务器是 `*.feishu.cn`；Lark 国际站换成对应的 CalDAV 地址即可（脚本不写死域名，用你给的 URL）。

### 飞书 CalDAV 的坑（给后来维护的人）

飞书 CalDAV 服务端不走常规路子，标准 `caldav` 库直接拿不到数据：

- `calendar-query` REPORT 只返回匹配的事件 href，**内联 `calendar-data` 返回 404**。
- 直接 `GET` 某个 `.ics` → **403**。
- **只有 `calendar-multiget` REPORT 能取到 ICS 内容。**

所以 `gather_calendar.py` 用原始 `requests` 手写流程：PROPFIND 发现日历 → calendar-query 拿当天 href →
calendar-multiget 批量取 ICS → 本地用 `recurring_ical_events` 展开 RRULE 并按当天本地时区筛选。
重复性日程（如每周会）服务端只回主事件(带 RRULE)，必须本地展开。

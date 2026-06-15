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

---

# 投递到飞书（webhook）+ 粘贴到文档

报告通过 `send_feishu.py` 投递，默认走**群自定义机器人 webhook**（没配则回退到 1:1 应用 DM）。

一次性配 webhook：

```bash
python3 ~/my/daily_report/send_feishu.py --set-webhook
# 粘 2 行：第1行 webhook URL、第2行 签名密钥（没有就留空）
```

存到 `.feishu_webhook.json`（权限 600，gitignored）。也可用环境变量 `FEISHU_WEBHOOK_URL` / `FEISHU_WEBHOOK_SECRET`。
报告以**蓝色卡片**发出（lark_md 渲染：加粗 Topic、`code`、每条 bullet 独立一行）。

## 粘贴到飞书云文档（实测 + 调研结论）

飞书「复制消息 → 粘到云文档」无法保证完全保真，要点：

- 在**飞书桌面客户端**里复制卡片再粘贴 → 保留**加粗 + 分行 + `code`**；**网页版**粘贴会**掉加粗**。
- 要 100% 完整：把归档的 `.md`（`/tq/scratch/thomas/daily_report_log/*.md`）**拖进飞书云空间导入**，自动转成带完整格式（加粗/列表/代码）的在线文档。
- 飞书**没有**「一键粘贴为 Markdown」快捷键；markdown 只在「逐字输入时自动转」或「文件导入」时才转换。
- 卡片用 lark_md **渲染**（不是源码/代码块/纯文本）：源码形态飞书粘贴会吃掉 `**`；纯文本粘贴丢全部格式；这些都试过，渲染卡片在桌面端粘贴最好。
- 别改用 `post`/富文本消息：自定义机器人不支持 post 的加粗，也没有原生列表。

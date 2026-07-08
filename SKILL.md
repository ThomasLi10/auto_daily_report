---
name: daily-report
description: 输入一个日期，结合当天的 git 提交历史和 Claude Code 会话记录，生成一段简洁的 daily report bullet 总结。用户说"写 daily report / 日报 / 总结某天工作 / daily-report <date>"时触发。
---

# Daily Report Skill

根据用户给定的某一天（本地时区），把当天的**实际产出（git commits）**和**讨论/调试过程（Claude Code 会话）**汇总成一段简洁的 daily report bullet。

## 输入

- 一个日期，格式 `YYYY-MM-DD`（本地时区）。
- 如果用户没给日期，默认用**昨天**；如果用户说"今天"就用今天。先用 `date +%F` 确认当前日期再做减法。
- 也支持**日期区间**（用户说"总结这周 / 这次假期 / X 到 Y"时）：给两个收集脚本都传 `<start> <end>`（含两端），最后写成**一份**跨区间的报告（标题用 `# Daily Report — <start> → <end>`）。

## 步骤

### 1. 收集原料

运行收集脚本（按本地时区过滤，自动发现 `~/code/*` 下的 git repo，并扫描 `~/.claude/projects` 的会话记录）。**先加载站点本地配置**（额外账号、要排除的自动化 session cwd），这样交互式生成和 cron 口径一致：

```bash
set -a; [ -f ~/my/daily_report/daily_report.local ] && . ~/my/daily_report/daily_report.local; set +a
python3 ~/my/daily_report/gather_context.py <YYYY-MM-DD>
```

可选参数：

- `--repos <path> ...` 指定要查的 git repo（默认自动发现 `~/code/*`）。
- `--projects-dir <path>` 指定 Claude 会话目录（默认 `~/.claude/projects`）。

脚本输出两块：

- **GIT COMMITS**：当天用户本人（按各 repo 的 `user.name` 过滤）在所有分支上的提交，按时间排序。这是**确定性的产出**。
- **CLAUDE CODE SESSIONS**：当天有活动的每个会话，含 AI 生成的标题、用户输入的 prompt、以及编辑过的文件。这反映**意图、调试和讨论**（包括没有 commit 的探索）。

再拉一次当天的**飞书日程**（会议/日历），作为补充上下文：

```bash
python3 ~/my/daily_report/gather_calendar.py <YYYY-MM-DD>
```

- 正常会打印当天的日程列表（时间 + 标题）。
- 如果**退出码非 0**（stderr 会有 `NO_CONFIG` / `CALDAV_FAILED` 等），说明日历还没配好或拉取失败——**跳过日程段落，不要报错中断**，可在结尾顺带提醒一句"飞书日程未接入（见 FEISHU_SETUP.md）"。
- 日历走 CalDAV（飞书账号 + 专用密码），接入与服务端怪癖见同目录 `FEISHU_SETUP.md`。

### 2. 综合成 bullet

阅读脚本输出后，**按主题（而不是按 commit 或按 session）归类**，写出简洁的 daily report。

**固化的输出格式（用户已定，除非当场另有要求就照此执行）：**

- **语言：English。** 正文用英文（代码符号等保留原文）。仅当用户在当次对话里明确要求别的语言时才跟随。
- **整个报告就是一个 bullet 列表**：标题行 `# Daily Report — <DATE>` 之后，每一行都是 `- ` 开头的 bullet。**不要任何分段标题**（不要 `**Work**`，也不要 `**📅 Schedule**`）。
- **工作 bullet**：按主题归类，每条 `- **Topic**: ...`。正常 3–5 条，最多 8 条。
- **覆盖优先于细节**：工作多时**优先把大方面铺全**——每个主要方面都要提到，别让某一个大主题把别的方面挤掉；**绝不为了凑 3–5 条而砍掉真实的工作主题**（方面确实多就往 8 条放）。某个方面内容多时**粗粒度总结**：抓主干、把相近的点**合并成一句**（提到就好），不要逐条罗列每个改动。
- **简洁优先**：用短语抓重点，别堆长句和从句；一条主 bullet 一两行就够。
- **少提具体路径/脚本名**：临时诊断脚本、scratch 目录、文件路径（如 `scripts/scratch/.../foo.py`、"scripts 15–23"）**一般不写**，只在特别关键时提；但有意义的模块 / node / 字段名（如 `opt_replay`、`trading_status`、`r_last`）可以保留。
- **一条 bullet 涉及多件事时用 sub-bullet**：主 bullet 写主题，底下缩进 `  - ` 列具体子项（如一串排查结论、多个改动点）；只有单一一件事就不用 sub-bullet。
- **日程作为最后一个 bullet**（仅当 gather 退出码 0 且当天有日程）：把当天日历汇总成**一条** bullet，开头加 `📅 `：
  - **面试**（标题含「面试」/interview，或「一面 / 二面」等面试轮次）合并成**计数**：`2 interviews`（1 场写 `1 interview`），不写候选人、不写时间。
  - **会议**：**优先用会议本身的名字**——描述性的会议名直接用，翻成英文并去掉多余的结尾 "Meeting"（如「AI+金融周会」→"AI+Finance weekly"、「全员例会」→"All-hands"、"Quant Team Weekly Meeting"→"Quant Team Weekly"、"Quant Team Knowledge Share Meeting"→"Quant Team Knowledge Share"、"Chang 1-on-1 Weekly Meeting"→"1-on-1 with Chang Zhang"）。**已命名的会议别再堆一长串与会人**——名字本身已经够了（只在确有信息量时留 1–2 个人名）。**只有当标题是通用/无意义的**（如裸「会议」/默认名，或标题本身就是 "Meeting with <名>"）才退回写 `Meeting with <人名>`——人名用英文，中文转拼音（邹煜曈→Yutong Zou）。gather 输出里 `｜with: 中文名` 给出与会人（已排除你自己）。
  - 多项用 `; ` 连接，例如 `- 📅 2 interviews; Meeting with Yutong Zou`。
  - 没有日程就**不要这条 bullet**。

写作要点：

- **以 git commits 为骨架**说明"做成了什么"，用会话 prompt 补充"为什么做 / 调试了什么 / 讨论了什么"。
- **同一主题的多个 commit 和会话合并成一条 bullet**（例如：trading_status 的多次提交 + 相关讨论 = 一条 "limit-up tradability modeling"）。
- **过滤掉与工作无关的内容**（个人咨询、闲聊、概念科普等纯学习性对话），除非用户要求全都包含。
- **不要结尾的 `(N commits)`**。

### 3. 输出

直接把报告贴给用户。然后简短地问一句是否需要调整，不要主动追加额外操作。

## 风格参考

```markdown
# Daily Report — 2026-05-29
- **prod vs iter1 PnL gap (~28pp)**: isolated the cause by elimination
  - ruled out: participation limit, slippage, cost structure, weight overlap, alpha capture, execution VWAP
  - prime suspect: γ scale (0.01 vs 100) driving different TE utilization; TE audit prepared, not yet run
- **PnL leg decomposition**: split sim PnL into night/day/trading — day dominant, night negative recently; reviewed `r_last` definition (open-to-close vs open-to-now)
- **Limit-up/down & participation**: optimizer only blocks fully-locked (status 1), lets pinned-but-open (status 3) through; added 10% participation cap on 10–11am volume to iter1 sim
- **opt_replay arena node**: replays iter1 weights through the prod sim pipeline (bypasses opt_comb); configs for cn_equity/estu_x/debench
- **Handoff docs**: elimination chain + next steps for continuation
- 📅 All-hands with Zhihui Chu & Naive AI; Quant Team Weekly; 1-on-1 with Chang Zhang
```

要点：第一条多件事 → 用 sub-bullet；其它一件事 → 一行；不写诊断脚本路径/编号。末尾会议优先用会议名（如 `Quant Team Weekly`、`1-on-1 with Chang Zhang`）；只有会议的日子就单一条 `- 📅 <会议名>`，标题通用时才退回 `Meeting with <人名>`。

## 注意

- **多用户来源**：`gather_context.py` 默认只采集当前用户，但可额外通过 `sudo -n -u <user>` 把**服务账号**的 git 提交（`/home/<user>/code/*`）和 Claude 会话（`/home/<user>/.claude/projects`）一并纳入——例如某服务账号下用 claude 做的 `report_hub` 等工作就能这样进来。默认不纳入任何额外账号，用环境变量 `DAILY_REPORT_EXTRA_USERS="acct1 acct2"`（cron 经 gitignored 的 `daily_report.local` 注入）或 `--extra-users a b` 指定。同一 commit 在两个 clone 里只算一次（按 hash 去重）；sudo 不可用时该来源跳过并打一行 `# NOTE`，不报错。会话/提交会带 `[<account>]` 标签，归类时把它和当前用户的同主题工作**合并**，别因账号不同拆成两条。要改/禁用：`--extra-users a b` / 裸 `--extra-users`。
- **剔除自动化流水线 session**：排队跑的自动任务（如 tq_ai 的 **library** / **alphas** mining）会在每个 job 的临时 checkout 里起 claude，一天可能几百个，会淹没日报。按 **cwd** 匹配 `DAILY_REPORT_EXCLUDE_CWD_GLOBS`（cron 经 `daily_report.local` 注入，如 `/tmp/tqlib_ro_* /tmp/aha_ro_*`）或 `--exclude-cwd` 把它们剔除，**只保留交互式（Claude-Max）的工作**；这对**所有来源**生效（本人账号下也会跑这些自动任务）。注意只匹配**临时 scratch 目录**，别误伤开发流水线代码本身的目录（如 `.../tq_ai/agent/alphas`）。被剔除的 job 其 **git commit 仍照常计入**（git 单独采集）；剔除时打一行 `# NOTE`。裸 `--exclude-cwd` = 不过滤。
- **自动化（cron）行为**：`run_daily_report.sh` 无参数时按**中国工作日**门控（`workday.py`，含调休）——工作日才综合，且覆盖「上次报告之后 → 昨天」整段（假期/周末后第一个工作日合并成一份）；非工作日发一条 rest-day 占位指向下个工作日。状态游标 `last_covered` 仅在成功发送后推进。交互式 `/daily-report` **不**受此门控，按用户给的日期/区间直接生成。
- 会话时间戳是 UTC，脚本已自动转换成本地时区再按日期过滤；git 的 `--since/--until` 用本地时间。两者口径一致。
- 一天可能有十几个会话，prompt 很多——**抓主题，不要逐条复述**。被中断的 prompt、纯粘贴的终端输出只作背景参考。
- 如果某天既没有 commit 也没有会话活动，如实告诉用户当天没有记录到工作内容。

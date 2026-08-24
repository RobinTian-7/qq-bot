# QQ 群日报 Bot

每天自动把班级群里老师发的消息整理成一份「扫一眼就知道要干什么」的日报。

```
QQ 群  →  NapCat（协议端）  →  常驻监听存 SQLite
                                      ↓
                            提取消息里的所有链接
                            （含公众号分享卡片、合并转发）
                                      ↓
                            抓网页正文 → 可按深度跟进子链接
                            （HTML / PDF / 微信公众号）
                                      ↓
      DeepSeek API  或  本机 codex CLI  结构化摘要
                                      ↓
                     reports/2026-08-24.md  +  .html
```

摘要后端有两个，`config.toml` 里一行切换，两个都实测跑通过：

| provider | 走什么 | 需要什么 | 结构化输出 |
|---|---|---|---|
| `deepseek` | DeepSeek API（OpenAI 兼容接口） | `DEEPSEEK_API_KEY` | json 模式 + 本地 pydantic 校验，不合格自动重试一次 |
| `codex` | 本机 `codex exec` | `codex login` 过的登录态 | `--output-schema` 由服务端强制，更硬 |

---

## 一、装依赖

```bash
git clone https://github.com/RobinTian-7/qq-bot.git
cd qq-bot
uv venv --python 3.12
uv pip install -e .
```

## 二、配摘要后端（二选一）

**用 DeepSeek**（默认）：

```bash
cp .env.example .env
# 编辑 .env，填上 https://platform.deepseek.com/api_keys 拿到的 key
```

**用 Codex**（有 Codex 订阅、不想再付 API 钱就选这个）：

```bash
codex login          # 确认已登录
```

然后把 `config.toml` 里改一行：

```toml
[summary]
provider = "codex"
```

想临时对比两边的效果，不用改配置：

```bash
.venv/bin/qq-agent report --provider deepseek
.venv/bin/qq-agent report --provider codex
```

## 三、跑起 NapCat（QQ 协议端）

NapCat 负责登录 QQ 并把群消息以 OneBot 11 协议转出来。**强烈建议用一个小号**，
不要用你的主号——协议端登录有被腾讯风控的可能。

```bash
docker compose up -d
docker compose logs -f napcat        # 日志里会打出登录二维码，手机 QQ 扫码
```

登录成功后打开 WebUI `http://127.0.0.1:3000`（默认 token 在容器日志里），
到「网络配置」里**新建一个 WebSocket 服务器**：

| 项目 | 值 |
|---|---|
| 类型 | WebSocket 服务器（正向） |
| 主机 | `0.0.0.0` |
| 端口 | `3001` |
| Token | 自己设一个，或留空 |
| 上报自身消息 | 关 |
| 启用 | 开 |

> 用 Lagrange.OneBot 也一样——它同样是 OneBot 11，把 `ws_url` 指过去就行，本项目代码不用改。

## 四、配置本项目

```bash
cp config.example.toml config.toml
```

必填的只有群号：

```toml
[group]
group_ids = [你的群号]
```

「谁算老师」有三种判定方式，满足任意一条即可，可以叠加用：

```toml
teacher_qqs = [10001, 10002]                    # ① QQ 号白名单（最准）
include_admins = true                            # ② 群主 / 管理员自动算
teacher_name_keywords = ["老师", "班主任"]        # ③ 群名片含关键词
```

三个全空/全关 = 收录群里所有人的消息。

如果 NapCat 那边设了 token，这里也要填：

```toml
[onebot]
ws_url = "ws://127.0.0.1:3001"
access_token = "你设的 token"
```

## 五、启动

```bash
.venv/bin/qq-agent run
```

这一条命令同时做两件事：实时监听群消息存库、每天 `daily_at`（默认 21:30）自动生成日报。
日报会写到 `reports/2026-08-24.md` 和 `reports/2026-08-24.html`。

想让它开机自启，用 launchd（`$PWD` 会展开成项目的绝对路径）：

```bash
cat > ~/Library/LaunchAgents/com.local.qq-agent.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.local.qq-agent</string>
  <key>ProgramArguments</key><array>
    <string>$PWD/.venv/bin/qq-agent</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/qq-agent.log</string>
  <key>StandardErrorPath</key><string>/tmp/qq-agent.err</string>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/com.local.qq-agent.plist
```

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `qq-agent run` | 常驻：监听 + 每天定点出日报（日常就用这个） |
| `qq-agent watch` | 只监听存库，不出日报 |
| `qq-agent report` | 立刻出一份今天的日报 |
| `qq-agent report -d 2026-08-20` | 补出某一天的日报（消息已在库里就行） |
| `qq-agent report --dry-run` | 不调 API，把送给模型的内容导出成 `.prompt.txt` 看一眼 |
| `qq-agent report --no-cache` | 强制重抓所有链接（网页更新了的时候用） |
| `qq-agent backfill -n 200` | Bot 掉线后从协议端补抓历史消息 |
| `qq-agent fetch <url>` | 单独试抓一个链接，看正文提取效果 |
| `qq-agent stats` | 看库里攒了多少数据 |

## 不连 QQ 先试一遍

```bash
.venv/bin/python scripts/seed_demo.py                      # 塞几条假的老师消息
.venv/bin/qq-agent -c config.demo.toml report --dry-run    # 不花钱，看输入
.venv/bin/qq-agent -c config.demo.toml report              # 真出日报
.venv/bin/python scripts/preview_report.py                 # 只看排版，不跑流程
```

---

## 几个会影响效果的配置

### 链接跟进深度 `[fetch].max_depth`

- `1`（默认）：只读老师发的那个页面。
- `2`：再跟进该页面里的同域链接。适合老师发的是「通知列表页」而不是具体通知的情况。
  挑子链接时会按路径深度、是否含文号/日期数字、锚文本长度打分，导航和页脚会被排掉，
  但仍是启发式的——跟进结果可以用 `-v` 看日志确认。

`max_children_per_page` 控制每页最多跟进几个（默认 5）。深度和数量都别调太大，抓取量是乘法关系。

### 内容量与花费 `[fetch].max_chars_per_page` / `max_total_chars`

单页超长会截断，并在日报里明确标出「原文较长，建议点开链接确认」。
一天总量超 `max_total_chars` 时，会从最长的页面开始丢弃，并在日报的「备注」里如实列出丢了哪些——
不会静默吞掉内容。送进模型前还会先 `count_tokens` 报一次预估花费；
超过 80 万 token 会直接停下来让你调小配置，而不是硬塞。

一个正常班级群一天大概 2–8 万 token。按 DeepSeek 高峰价：

| 模型 | 输入 $/1M | 输出 $/1M | 一天大概 |
|---|---|---|---|
| `deepseek-v4-pro` | 1.32 | 3.96 | 几分到两毛 |
| `deepseek-v4-flash` | 0.44 | 1.32 | 几分钱 |

非高峰时段（UTC 01:00–04:00、06:00–10:00 之外）是半价。DeepSeek 的磁盘缓存是自动的，
同一天重跑 `report` 命中缓存后输入几乎免费，日志里 `缓存命中` 那一栏能看到。

`reasoning_effort` 影响很大——上面那次 2 千 token 的输入，思考就花了 2.8 千输出 token。
嫌贵可以调成 `low`，但相对日期（"本周五"→具体日期）的推算准确率会下降，这是它最有价值的部分。

用 `codex` 后端则不走 API 计费，吃你 Codex 订阅的额度。

### 换模型

`[summary.deepseek].model` 可填 `deepseek-v4-pro`（默认，更准）或 `deepseek-v4-flash`（便宜约 3 倍）。
`[summary.codex].model` 留空就是 codex 自己的默认模型。

### 统计区间 `[report].daily_at` / `window_hours`

日报覆盖的是 `[daily_at - window_hours, daily_at)`。默认 21:30 + 24 小时 =
「昨晚 21:30 到今晚 21:30」，符合晚上看当天汇总的习惯。

### 抓不到的网页

- 百度网盘、需要登录的页面：抓不了，会在日报「备注」里提示你手动点开。可以往 `blocked_domains` 里加。
- 微信公众号文章：能抓（`mp.weixin.qq.com`），走 trafilatura + `#js_content` 兜底。
  少数带防盗链/需要在微信内打开的会失败。
- PDF：能抓，纯扫描件（图片 PDF）提不出文字，会报「没抓到正文」。
- `respect_robots = true` 时会遵守 robots.txt；某些学校站点 robots 写得很严，
  确认是自己有权访问的内容再考虑关掉。

---

## 关于准确性

日报是模型生成的，system prompt 里已经写死了几条硬约束：

- 只写原文里真实存在的内容，日期/金额/地点不许推测，信息不全就写「原文未说明」
- 「明天」「本周五」这类相对时间会按当天日期换算成 `YYYY-MM-DD`，并在摘要里注明推算依据
- `source_urls` 必须逐字复制输入里出现过的 URL，不许自己拼
- 正文被截断的条目会主动提示「建议点开链接确认」

即便如此，**涉及缴费、考试时间、材料截止这类事情，请点开原文链接再确认一遍**。
每个条目都带了原始链接和来源消息编号（`#9001`），就是为了让你能一键回溯。

## 隐私

群消息全部存在本机 `data/qq_agent.db`，只有生成日报时才会把当天的消息和网页正文发出去——
`provider = "deepseek"` 时发给 DeepSeek，`provider = "codex"` 时经由本机 codex CLI 发给 OpenAI。
`.gitignore` 里已经排除了 `data/`、`reports/`、`.env`、`config.toml` 和 `napcat/`。

## 目录结构

```
src/qq_agent/
  config.py       配置加载
  db.py           SQLite（messages / pages / digests）
  onebot.py       OneBot 11 WebSocket 客户端
  message.py      消息解析、CQ 码、分享卡片抽链接
  collector.py    常驻监听 + 老师判定 + 合并转发展开
  fetcher.py      网页抓取、正文提取、子链接跟进
  summarizer.py   日报数据结构、提示词、后端选择
  backends/
    deepseek.py   DeepSeek API（OpenAI 兼容接口）
    codex.py      调本机 codex exec --output-schema
  render.py       Markdown / HTML / QQ 纯文本渲染
  pipeline.py     日报生成流水线
  cli.py          命令行
  templates/report.html.j2
scripts/
  seed_demo.py       塞演示数据
  preview_report.py  只渲染排版
```

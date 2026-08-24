"""不调 API，用一份示例数据渲染日报，看看排版长什么样。

用法：.venv/bin/python scripts/preview_report.py  →  reports/demo/preview.html
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qq_agent.render import to_html, to_markdown, to_qq_text   # noqa: E402
from qq_agent.summarizer import Digest, DigestItem             # noqa: E402

DEMO = Digest(
    headline="明天体检要空腹，周五前交社会实践登记表",
    urgent=["今晚把社会实践登记表打印出来给家长签字", "明早不要吃早饭，7:50 前到体育馆"],
    items=[
        DigestItem(
            title="8月25日全校体检，需空腹",
            category="通知", importance="高", deadline="2026-08-25",
            summary="王老师转发校医院通知，8 月 25 日上午全校体检，要求空腹到校，"
                    "8:00 在体育馆集合。补检安排原文未说明。",
            key_points=["8:00–11:30 体育馆", "空腹，可自带早餐检后吃", "带学生证和口罩"],
            actions=["8 月 25 日早上不要吃早饭", "带上学生证和口罩"],
            source_message_ids=[9001, 9002],
            source_urls=["https://example.edu.cn/notice/2026/0824.html"],
        ),
        DigestItem(
            title="社会实践登记表本周五截止",
            category="材料提交", importance="高", deadline="2026-08-28",
            summary="李老师要求本周五（按今天周一推算为 8 月 28 日）前把暑期社会实践登记表"
                    "交给班长，需家长签字。表格在附件链接。（原文较长，建议点开链接确认完整内容）",
            key_points=["需家长签字", "统一交给班长汇总"],
            actions=["打印登记表并请家长签字", "8 月 28 日前交给班长"],
            source_message_ids=[9003],
            source_urls=["https://example.edu.cn/files/shijian-form.pdf"],
        ),
        DigestItem(
            title="下周三家长会改为线上",
            category="活动", importance="中", deadline="2026-08-27",
            summary="原定线下的家长会改为腾讯会议，会议链接当天再发群里。",
            key_points=["19:30 开始"], actions=[],
            source_message_ids=[9005], source_urls=[],
        ),
        DigestItem(
            title="班主任转发的开学寄语",
            category="其他", importance="低", deadline="",
            summary="一篇鼓励性质的公众号文章，不需要做任何事。",
            key_points=[], actions=[], source_message_ids=[9006],
            source_urls=["https://mp.weixin.qq.com/s/example"],
        ),
    ],
    notes="有 1 个链接指向百度网盘（在黑名单里），没有抓取，需要手动点开确认。",
)
META = {"n_messages": 27, "n_pages_ok": 3, "n_pages_failed": 1, "n_pages_dropped": 0}

out = Path(__file__).resolve().parents[1] / "reports" / "demo"
out.mkdir(parents=True, exist_ok=True)
(out / "preview.html").write_text(to_html("2026-08-24", DEMO, META), "utf-8")
(out / "preview.md").write_text(to_markdown("2026-08-24", DEMO, META), "utf-8")
(out / "preview.qq.txt").write_text(to_qq_text("2026-08-24", DEMO), "utf-8")
print("已生成：", *(str(out / f) for f in ("preview.html", "preview.md", "preview.qq.txt")), sep="\n  ")

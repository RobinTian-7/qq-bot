"""往数据库里塞几条假的老师消息，用来跑通流程（不需要真的连 QQ）。

用法：.venv/bin/python scripts/seed_demo.py [config.toml]
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qq_agent.config import Config          # noqa: E402
from qq_agent.db import Store               # noqa: E402
from qq_agent.message import extract_urls, normalize_segments, segments_to_text  # noqa: E402

cfg = Config.load(sys.argv[1] if len(sys.argv) > 1 else "config.demo.toml")
store = Store(cfg.resolve(cfg.storage.db_path))
gid = cfg.group.group_ids[0]
now = int(time.time())

DEMO = [
    (9001, 10001, "王老师", "owner",
     "各位家长好，下周一（8月25日）全校体检，请学生空腹到校，详情见 https://www.python.org/psf/annual-report/2024/ ，8:00 在体育馆集合。"),
    (9002, 10001, "王老师", "owner", "补充一下：记得带学生证和口罩。"),
    (9003, 10002, "李老师", "admin",
     "暑期社会实践登记表请本周五前交给班长，需要家长签字。表格下载：https://www.python.org/blogs/"),
    (9004, 10055, "张同学家长", "member", "收到，谢谢老师！"),
    (9005, 10002, "李老师", "admin", "下周三的家长会改成线上，腾讯会议链接当天发群里。"),
]

for i, (mid, uid, name, role, text) in enumerate(DEMO):
    segs = normalize_segments(text)
    store.save_message({
        "message_id": mid, "group_id": gid, "user_id": uid,
        "sender_name": name, "sender_role": role,
        "is_teacher": role in ("owner", "admin"),
        "ts": now - (len(DEMO) - i) * 600,
        "text": segments_to_text(segs), "raw": segs, "urls": extract_urls(segs),
    })
print(f"已写入 {len(DEMO)} 条演示消息到群 {gid}：", store.stats())
store.close()

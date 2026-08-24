"""Codex 后端：把整理任务交给本机的 codex CLI。

用 `codex exec --output-schema` 拿结构化输出——schema 由 OpenAI 侧强制，
比 DeepSeek 的 json 模式更硬。适合手上有 Codex 订阅、不想额外付 API 费的情况。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import Config
from ..fetcher import Page
from ..summarizer import (
    BaseSummarizer,
    Digest,
    estimate_tokens,
    parse_digest,
    strict_schema,
)

log = logging.getLogger("qq_agent.codex")

# codex 是个 agent，默认会想着去读文件、跑命令。这里明确按住它。
GUARD = """

补充约束（针对本次运行）：
- 这是一个纯文本整理任务。不要执行任何命令，不要读写任何文件，不要联网。
- 你需要的全部信息都在下面的输入里。直接按 output schema 给出结果。
"""


class CodexSummarizer(BaseSummarizer):
    name = "codex"

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self.cc = cfg.summary.codex
        self.bin = shutil.which(self.cc.command)
        if not self.bin:
            raise SystemExit(
                f"找不到 `{self.cc.command}` 命令。\n"
                "  装：npm i -g @openai/codex   然后 codex login\n"
                "  或者把 config.toml 里 [summary].provider 改回 deepseek"
            )

    def summarize(self, day: str, msgs: list[dict], pages: list[Page]) -> tuple[Digest, dict]:
        # schema 由 --output-schema 强制，提示词里不用再塞一遍
        content, meta = self.build_prompt(day, msgs, pages)
        prompt = self.system_prompt(with_json_rules=False) + GUARD + "\n\n" + content

        n_tok = estimate_tokens(prompt)
        meta["input_tokens_estimated"] = n_tok
        log.info("送进 codex 的内容约 %s tokens（估算）", f"{n_tok:,}")

        with tempfile.TemporaryDirectory(prefix="qq-agent-codex-") as tmp:
            tmpd = Path(tmp)
            schema_f = tmpd / "schema.json"
            out_f = tmpd / "digest.json"
            schema_f.write_text(json.dumps(strict_schema(), ensure_ascii=False), "utf-8")

            cmd = [
                self.bin, "exec",
                "--ephemeral",             # 不往磁盘写 session
                "--skip-git-repo-check",
                "-s", self.cc.sandbox,     # 默认 read-only
                "-C", str(tmpd),           # 工作目录隔离到临时目录
                "--output-schema", str(schema_f),
                "-o", str(out_f),
                "--color", "never",
                "-",                       # prompt 走 stdin
            ]
            if self.cc.model:
                cmd[2:2] = ["-m", self.cc.model]

            log.info("调用 codex（最多等 %d 秒）…", self.cc.timeout)
            try:
                proc = subprocess.run(
                    cmd, input=prompt, text=True, capture_output=True,
                    timeout=self.cc.timeout,
                )
            except subprocess.TimeoutExpired:
                raise SystemExit(
                    f"codex 超过 {self.cc.timeout} 秒还没返回。"
                    f"把 [summary.codex].timeout 调大，或改用 deepseek。"
                ) from None

            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip()[-800:]
                hint = ""
                if "login" in tail.lower() or "auth" in tail.lower():
                    hint = "\n看起来是没登录，先跑一次 `codex login`。"
                raise SystemExit(f"codex 退出码 {proc.returncode}：\n{tail}{hint}")

            if not out_f.exists():
                tail = (proc.stdout or "").strip()[-800:]
                raise SystemExit(f"codex 没有产出结果文件。最后的输出：\n{tail}")
            raw = out_f.read_text("utf-8")

        digest = parse_digest(raw)
        meta["model"] = self.cc.model or "(codex 默认)"
        meta["usage"] = {}
        return digest, meta

"""DeepSeek 后端。DeepSeek 提供 OpenAI 兼容接口，所以直接用 openai SDK。

注意 DeepSeek 的 JSON 模式（response_format={"type":"json_object"}）只保证输出是
合法 json，不做 schema 校验——schema 靠提示词约束 + 本地 pydantic 校验 + 失败重试。
"""
from __future__ import annotations

import logging
import os

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)
from pydantic import ValidationError

from ..config import Config
from ..fetcher import Page
from ..summarizer import (
    BaseSummarizer,
    Digest,
    estimate_tokens,
    parse_digest,
)

log = logging.getLogger("qq_agent.deepseek")

# 官网价目表（美元 / 百万 token，高峰价；非高峰时段是一半）
# 01:00-04:00 和 06:00-10:00 UTC 之外算非高峰。
PRICING = {
    "deepseek-v4-pro":              {"in": 1.32, "out": 3.96},
    "deepseek-v4-flash":            {"in": 0.44, "out": 1.32},
    "deepseek-v4-flash-vision-exp": {"in": 0.44, "out": 1.32},
}


class DeepSeekSummarizer(BaseSummarizer):
    name = "deepseek"

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self.dc = cfg.summary.deepseek
        key = os.environ.get(self.dc.api_key_env, "").strip()
        if not key:
            raise SystemExit(
                f"环境变量 {self.dc.api_key_env} 是空的。\n"
                "  cp .env.example .env  然后填上 DeepSeek 的 key\n"
                "  key 在 https://platform.deepseek.com/api_keys 拿"
            )
        self.client = OpenAI(api_key=key, base_url=self.dc.base_url, timeout=self.dc.timeout)

    # ---------- 内部 ----------

    def _create(self, messages: list[dict]) -> tuple[str, dict]:
        kwargs: dict = {
            "model": self.dc.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_tokens": self.dc.max_tokens,
            "stream": False,
        }
        if self.dc.thinking:
            kwargs["reasoning_effort"] = self.dc.reasoning_effort
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        try:
            resp = self.client.chat.completions.create(**kwargs)
        except AuthenticationError:
            raise SystemExit(f"{self.dc.api_key_env} 无效，去 platform.deepseek.com 重新生成。") from None
        except RateLimitError:
            raise SystemExit("DeepSeek 限流了，等一会儿再跑 `qq-agent report`。") from None
        except APIConnectionError as e:
            raise SystemExit(f"连不上 DeepSeek（{self.dc.base_url}），检查网络/代理。\n{e}") from None
        except APIStatusError as e:
            raise SystemExit(f"DeepSeek 返回 {e.status_code}：{str(e)[:300]}") from None

        usage = resp.usage.model_dump() if resp.usage else {}
        choice = resp.choices[0] if resp.choices else None
        content = (choice.message.content if choice and choice.message else "") or ""
        if choice and choice.finish_reason == "length":
            log.warning("输出被 max_tokens 截断，json 多半不完整；把 [summary.deepseek].max_tokens 调大。")
        return content, usage

    @staticmethod
    def _log_usage(usage: dict, model: str) -> None:
        hit = usage.get("prompt_cache_hit_tokens", 0) or 0
        miss = usage.get("prompt_cache_miss_tokens", 0) or usage.get("prompt_tokens", 0) or 0
        out = usage.get("completion_tokens", 0) or 0
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        price = PRICING.get(model)
        cost = ""
        if price:
            # 缓存命中大约是 miss 价的 1/30，量小时忽略不计
            cost = f"，高峰价约 ${(miss / 1e6) * price['in'] + (out / 1e6) * price['out']:.3f}"
        log.info(
            "用量：输入 %s（缓存命中 %s）/ 输出 %s（其中思考 %s）%s",
            f"{miss + hit:,}", f"{hit:,}", f"{out:,}", f"{reasoning:,}", cost,
        )

    # ---------- 对外 ----------

    def summarize(self, day: str, msgs: list[dict], pages: list[Page]) -> tuple[Digest, dict]:
        content, meta = self.build_prompt(day, msgs, pages)
        system = self.system_prompt(with_json_rules=True)

        n_tok = estimate_tokens(system + content)
        meta["input_tokens_estimated"] = n_tok
        price = PRICING.get(self.dc.model)
        est = f"，输入约 ${n_tok / 1e6 * price['in']:.3f}" if price else ""
        log.info("送进 %s 的内容约 %s tokens（估算）%s", self.dc.model, f"{n_tok:,}", est)
        if n_tok > self.dc.max_input_tokens:
            raise SystemExit(
                f"今天的内容估算约 {n_tok:,} tokens，超过设定上限 "
                f"{self.dc.max_input_tokens:,}，没有继续。\n"
                f"把 config.toml 里 [fetch].max_chars_per_page 或 max_total_chars 调小后重跑。"
            )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        raw, usage = self._create(messages)
        self._log_usage(usage, self.dc.model)

        try:
            digest = parse_digest(raw)
        except (ValidationError, ValueError) as e:
            # 官方文档也承认 json 模式偶尔返回空内容，重试一次并把错误喂回去
            log.warning("第一次输出不合 schema，重试一次：%s", str(e)[:200])
            messages += [
                {"role": "assistant", "content": raw or "(空)"},
                {"role": "user", "content":
                    f"上面的输出不符合要求：{str(e)[:800]}\n"
                    f"请重新只输出一个合法的 json 对象，严格符合前面给的 JSON Schema。"},
            ]
            raw2, usage2 = self._create(messages)
            self._log_usage(usage2, self.dc.model)
            digest = parse_digest(raw2)
            usage = usage2
            meta["retried"] = True

        meta["usage"] = usage
        meta["model"] = self.dc.model
        return digest, meta

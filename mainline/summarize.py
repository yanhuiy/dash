"""可选的 LLM 一句话摘要与情绪分类（只做摘要，不预测涨跌）。"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

PROMPT = (
    "你是金融文本摘要器。请仅根据给定标题与正文，输出 JSON，不要预测涨跌、不要给投资建议。"
    "格式：{\"summary\":\"不超过50字的客观摘要\",\"sentiment\":\"likely_positive|neutral|negative|uncertain\"}。"
)


def llm_enrich(
    items: list[dict[str, Any]],
    config: dict[str, Any],
    logger,
    max_items: int = 6,
) -> None:
    cfg = config.get("ai", {})
    if not cfg.get("enabled"):
        return
    api_key = os.getenv(str(cfg.get("api_key_env", "OPENAI_API_KEY")))
    if not api_key:
        logger.warning("AI 已启用但未找到 API Key，继续使用规则摘要")
        return

    endpoint = str(cfg.get("base_url", "")).rstrip("/") + "/chat/completions"
    for item in items[:max_items]:
        try:
            resp = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": cfg.get("model", "gpt-4o-mini"),
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": PROMPT},
                        {
                            "role": "user",
                            "content": f"标题：{item['title']}\n正文：{item.get('summary', '')}",
                        },
                    ],
                },
                timeout=int(cfg.get("timeout", 30)),
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if parsed.get("summary"):
                item["summary"] = str(parsed["summary"])[:120]
            if parsed.get("sentiment") in {"likely_positive", "neutral", "negative", "uncertain"}:
                item["sentiment"] = parsed["sentiment"]
        except Exception as exc:  # AI 失败不影响主流程
            logger.warning("AI 摘要失败：%s", type(exc).__name__)

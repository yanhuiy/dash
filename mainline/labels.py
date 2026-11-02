"""展示用中文标签。"""

STATUS_LABELS = {
    "observe": "观察",
    "strengthen": "强化",
    "crowded": "拥挤",
    "weaken": "走弱",
    "switch_candidate": "切换候选",
    "switch_confirmed": "切换确认",
}

SENTIMENT_LABELS = {
    "likely_positive": "利好",
    "neutral": "中性",
    "negative": "利空",
    "uncertain": "不确定",
}

TIER_LABELS = {"core": "核心池", "watch": "观察池", "retired": "已退役"}

CATEGORY_LABELS = {"policy": "政策", "research": "研报", "news": "快讯"}


def status_label(value: str) -> str:
    return STATUS_LABELS.get(value, value)


def sentiment_label(value: str) -> str:
    return SENTIMENT_LABELS.get(value, value)

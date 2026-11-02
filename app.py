"""市场主线切换盘后看板（Streamlit 单页，按日期归档）。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from mainline import db
from mainline.config import load_config, load_themes
from mainline.directions import BASIS_LABELS, choose_directions, conclusion
from mainline.labels import CATEGORY_LABELS, TIER_LABELS, sentiment_label, status_label

st.set_page_config(page_title="市场主线切换盘后看板", layout="wide")

CONFIG = load_config()
DB_PATH = CONFIG["output"]["sqlite_path"]
THEMES = load_themes(CONFIG)
THEME_NAMES = {theme.id: theme.name for theme in THEMES}


@st.cache_data(show_spinner=False, ttl=60)
def load_dates(path: str) -> list[str]:
    if not Path(path).exists():
        return []
    conn = db.connect(path)
    try:
        return db.list_dates(conn)
    except Exception:
        return []
    finally:
        conn.close()


@st.cache_data(show_spinner=False, ttl=60)
def load_day(path: str, date_str: str) -> dict:
    conn = db.connect(path)
    try:
        return db.load_day(conn, date_str)
    finally:
        conn.close()


def pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.{digits}f}%"


def num(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def split_themes(value: str | None) -> list[str]:
    if not value:
        return []
    return [part for part in value.split(",") if part]


def quote_latest(symbol: str, value: float | None) -> str:
    if value is None:
        return "—"
    if symbol == "US10Y":
        return f"{value:.2f}%"
    if symbol == "fx_susdcny":
        return f"{value:.4f}"
    return f"{value:,.2f}"


def quote_change(symbol: str, value: float | None) -> str:
    if value is None:
        return "—"
    if symbol == "US10Y":
        return f"{value:+.2f}pct"
    return f"{value:+.2f}%"


def render_news(item: dict) -> None:
    with st.container(border=True):
        st.markdown(f"**[{item['title']}]({item['url']})**")
        themes = "、".join(THEME_NAMES.get(tid, tid) for tid in split_themes(item.get("matched_themes")))
        st.caption(
            f"{item['source']} ｜ {item['published_at']} ｜ "
            f"{sentiment_label(item.get('sentiment', 'neutral'))} ｜ 影响主线：{themes or '未匹配'}"
        )
        if item.get("summary"):
            st.write(item["summary"])


def pick_items(items: list[dict], category: str, urls: list[str], limit: int) -> list[dict]:
    pool = [item for item in items if item["category"] == category]
    by_url = {item["url"]: item for item in pool}
    chosen = [by_url[url] for url in urls if url in by_url]
    if not chosen:
        pool.sort(key=lambda item: item.get("published_at", ""), reverse=True)
        chosen = pool
    return chosen[:limit]


dates = load_dates(DB_PATH)
st.title("市场主线切换盘后看板")

if not dates:
    st.warning("尚未生成任何快照。请先运行每日任务：")
    st.code("python run_daily.py --date 2026-10-23", language="bash")
    st.stop()

head_left, head_right = st.columns([4, 1])
with head_left:
    st.caption("盘后固定更新 ｜ 四区块：政策研报 · 美股期货 · 三大方向 · 持仓备注")
with head_right:
    selected = st.selectbox("日期", dates, index=0, label_visibility="collapsed")

data = load_day(DB_PATH, selected)
if not data:
    st.error("该日期快照缺失")
    st.stop()

payload = data["payload"]
themes = data["themes"]
theme_by_id = {row["theme_id"]: row for row in themes}

directions = payload.get("directions") or {}
if not directions.get("basis"):
    # 旧快照没有排名兜底结果，展示时按已存指标重算，避免三张卡全空。
    metrics = {}
    for row in themes:
        row = dict(row)
        try:
            row["extras"] = json.loads(row.get("extras_json") or "{}")
        except (TypeError, ValueError):
            row["extras"] = {}
        metrics[row["theme_id"]] = row
    directions = choose_directions(
        metrics, {tid: row["status"] for tid, row in metrics.items()}, {}
    )

news_items = data["news"]
quotes = data["quotes"]
limit_rows = data["limit_up"]
holdings = data["holdings"]

st.caption(f"快照日期 {data['date']} ｜ 生成时间 {data['generated_at']} ｜ 基准指数 {payload.get('baseline_index')}")

failures = payload.get("failed_sources") or []
if failures:
    st.warning("今日缺失/降级信源：" + "；".join(failures))

# ---------------- 区块 A ----------------
st.header("A. 公开政策与研报")
display = payload.get("display") or {}
policy_items = pick_items(
    news_items, "policy", display.get("policy", []), int(CONFIG.get("display_max_policy", 3))
)
research_items = pick_items(
    news_items, "research", display.get("research", []), int(CONFIG.get("display_max_research", 3))
)

col_policy, col_research = st.columns(2)
with col_policy:
    st.subheader("政策（最多3条）")
    if policy_items:
        for item in policy_items:
            render_news(item)
    else:
        st.info("今日无新增政策条目（该源今日缺失或未更新）")
with col_research:
    st.subheader("研报（最多3条）")
    if research_items:
        for item in research_items:
            render_news(item)
    else:
        st.info("今日无新增研报条目（该源今日缺失或未更新）")

catalyst_today = payload.get("catalyst_today") or {}
catalyst_5d = payload.get("catalyst_5d") or {}
nonzero = [(THEME_NAMES.get(tid, tid), count) for tid, count in catalyst_today.items() if count]
five_day_text = "，".join(
    f"{THEME_NAMES.get(tid, tid)} {count}" for tid, count in catalyst_5d.items() if count
)
st.caption(
    "今日催化计数："
    + ("；".join(f"{name} {count} 次" for name, count in nonzero) if nonzero else "无命中催化词的新增条目")
    + (f" ｜ 近5日：{five_day_text}" if five_day_text else "")
)

# ---------------- 区块 B ----------------
st.header("B. 美股期货走势")
st.caption("仅描述外围环境，不构成对A股涨跌的预测。")
if quotes:
    quote_rows = [
        {
            "品种": row["name"],
            "最新": quote_latest(row["symbol"], row["latest"]),
            "涨跌幅": quote_change(row["symbol"], row["change_pct"]),
            "行情时间": row["quote_time"] or "—",
            "对A股主线影响": row["note"] or "—",
        }
        for row in quotes
    ]
    st.dataframe(pd.DataFrame(quote_rows), hide_index=True, width="stretch")
else:
    st.warning("该源今日缺失：未获取到美股期货/美元/美债/商品/汇率行情。")

# ---------------- 区块 C ----------------
st.header("C. 三大方向")
st.caption("状态仍按规则判定；当日无对应状态触发时，按相对排名兜底展示。")
switch_meta = payload.get("switch")

cards = [
    ("当前最强主线", "strongest"),
    ("潜在切换方向", "switching"),
    ("走弱/风险方向", "weakening"),
]
columns = st.columns(3)
for column, (label, key) in zip(columns, cards):
    with column:
        st.subheader(label)
        theme_id = directions.get(key)
        row = theme_by_id.get(theme_id) if theme_id else None
        if row is None:
            st.info("暂无可排名的主线数据")
            continue
        with st.container(border=True):
            st.markdown(f"**{row['name']}** ｜ {status_label(row['status'])}")
            basis = (directions.get("basis") or {}).get(key)
            if basis and basis != "status":
                st.caption(f"{BASIS_LABELS.get(basis, basis)}（当日该状态未触发）")
            st.caption(
                f"RS {pct(row['rs'])} ｜ 量能 {num(row['volume_ratio'])}倍 ｜ "
                f"龙头宽度 {pct(row['breadth'], 0)} ｜ 催化 {row['catalyst_count']} 条"
            )
            st.caption(
                f"涨停 {row['limit_up_count'] if row['limit_up_count'] is not None else '—'} 家"
                f"/{row['limit_up_trend'] or '—'} ｜ 载体 {row['carrier']}"
            )
            st.write(conclusion(row, row["status"]))
            if switch_meta and switch_meta.get("new") == row["theme_id"]:
                old_name = THEME_NAMES.get(switch_meta.get("old"), switch_meta.get("old"))
                confirm_days = int(switch_meta.get("confirm_days", 2))
                verdict = (
                    f"连续{confirm_days}日成立，已确认"
                    if switch_meta.get("confirmed")
                    else "首日成立，待确认"
                )
                st.caption(f"切换对象：{old_name}；{verdict}")

st.subheader("切换雷达（仅列状态变化）")
radar = payload.get("radar") or []
if radar:
    radar_rows = [
        {
            "主线": row["name"],
            "昨日状态": status_label(row["prev"]),
            "今日状态": status_label(row["curr"]),
            "类型": row["change_type"],
            "说明": row["detail"],
        }
        for row in radar
    ]
    st.dataframe(pd.DataFrame(radar_rows), hide_index=True, width="stretch")
else:
    if payload.get("prev_date"):
        st.caption("今日无状态变化。")
    else:
        st.caption("首次快照，暂无可比昨日状态。")

st.subheader("涨停方向热度（仅作情绪验证）")
if limit_rows:
    limit_display = []
    for row in limit_rows:
        theme_names = "、".join(THEME_NAMES.get(tid, tid) for tid in split_themes(row["theme_ids"]))
        series = json.loads(row["series_json"]) if row.get("series_json") else []
        limit_display.append(
            {
                "细分方向": row["sector"],
                "今日涨停家数": row["count"],
                "连板家数": row["lianban"],
                "首板家数": row["first_board"],
                "近5日序列": " → ".join("—" if value is None else str(value) for value in series),
                "近5日趋势": row["trend"],
                "是否属于主线池": f"是（{theme_names}）" if row["in_theme_pool"] else "否",
                "备注": row["note"] or "—",
            }
        )
    st.dataframe(pd.DataFrame(limit_display), hide_index=True, width="stretch", height=420)
else:
    st.warning("涨停源今日缺失，或当日无涨停数据。")

with st.expander("全部主线指标与状态"):
    all_rows = []
    for row in themes:
        extras = json.loads(row["extras_json"]) if row.get("extras_json") else {}
        trend_bits = ["站上20日线" if row["trend_above_ma20"] else "跌破20日线"]
        if row["trend_golden_cross"]:
            trend_bits.append("5/10金叉")
        elif row["trend_ma5_over_ma10"]:
            trend_bits.append("5日线>10日线")
        else:
            trend_bits.append("5日线<10日线")
        all_rows.append(
            {
                "主线": row["name"],
                "池": TIER_LABELS.get(row["tier"], row["tier"]),
                "状态": status_label(row["status"]),
                "RS": pct(row["rs"]),
                "量能倍数": num(row["volume_ratio"]),
                "龙头宽度": pct(row["breadth"], 0),
                "龙头覆盖": f"{row['breadth_covered']}/{extras.get('leaders_total', '—')}",
                "催化(5日)": row["catalyst_count"],
                "涨停(家)": row["limit_up_count"] if row["limit_up_count"] is not None else "—",
                "5日涨幅": pct(row["ret_5d"]),
                "趋势": "；".join(trend_bits),
                "载体": row["carrier"],
                "备注": row["note"] or "—",
            }
        )
    st.dataframe(pd.DataFrame(all_rows), hide_index=True, width="stretch")

# ---------------- 区块 D ----------------
st.header("D. 持仓备注（纯展示，不参与计算）")
if holdings:
    holding_rows = [
        {
            "代码": row["code"],
            "名称": row["name"],
            "对应主线": row["theme_name"] or "—",
            "该主线今日状态": status_label(row["theme_status"]) if row["theme_status"] else "—",
            "风险备注": row["risk_note"] or "—",
            "映射状态": row["mapping_status"],
        }
        for row in holdings
    ]
    st.dataframe(pd.DataFrame(holding_rows), hide_index=True, width="stretch")
else:
    st.info("holdings.yaml 未配置持仓。")

st.divider()
st.caption(
    "数据来源：中国政府网、央行、证监会、统计局、东方财富研报、华尔街见闻、"
    "腾讯/新浪行情、东方财富涨停池。政策与研报仅保留公开链接；不抓付费全文。"
)
st.caption("本看板仅用于研究与信息整理，不构成任何投资建议；不对涨跌做预测。")

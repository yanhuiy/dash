"""把 SQLite 快照导出为静态 HTML，供 GitHub Pages 托管。

用法：
    python publish.py                 # 生成 docs/（最新页 + 历史归档页）
    python publish.py --date 2026-11-02

生成的页面完全自包含（内联 CSS/JS），不依赖 Streamlit 或后端服务。
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mainline import db
from mainline.config import load_config, load_themes
from mainline.directions import BASIS_LABELS, conclusion
from mainline.labels import TIER_LABELS, sentiment_label, status_label

WEEKDAYS = "一二三四五六日"


# ---------------------------------------------------------------- 基础工具


def esc(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value), quote=True)


def safe_url(value: Any) -> str | None:
    if not value:
        return None
    parsed = urlparse(str(value))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return html.escape(str(value), quote=True)


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


def date_label(value: str) -> str:
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return value
    return f"{value}（周{WEEKDAYS[day.weekday()]}）"


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


def status_badge(status: str | None) -> str:
    if not status:
        return '<span class="badge s-none">—</span>'
    return f'<span class="badge s-{esc(status)}">{esc(status_label(status))}</span>'


def sentiment_badge(value: str | None) -> str:
    if not value:
        return ""
    return f'<span class="tag">{esc(sentiment_label(value))}</span>'


def table(headers: list[str], rows: list[list[str]], cls: str = "") -> str:
    if not rows:
        return '<p class="empty">暂无数据</p>'
    head = "".join(f"<th>{esc(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return (
        f'<div class="table-wrap {cls}"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    ).replace('table-wrap "', 'table-wrap"')


# ---------------------------------------------------------------- 页面片段


def render_date_switcher(dates: list[str], selected: str, is_archive: bool) -> str:
    options = []
    for item in dates:
        if item == dates[0]:
            value = "../index.html" if is_archive else "index.html"
            label = f"最新 · {item}"
        else:
            value = f"{item}.html" if is_archive else f"date/{item}.html"
            label = date_label(item)
        current = " selected" if item == selected else ""
        options.append(f'<option value="{value}"{current}>{esc(label)}</option>')
    return (
        '<select class="date-select" aria-label="选择日期" '
        "onchange=\"if(this.value){location.href=this.value}\">"
        + "".join(options)
        + "</select>"
    )


def render_news(item: dict, theme_names: dict[str, str]) -> str:
    url = safe_url(item.get("url"))
    title = esc(item.get("title"))
    title_html = (
        f'<a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>'
        if url
        else title
    )
    themes = "、".join(theme_names.get(tid, tid) for tid in split_themes(item.get("matched_themes")))
    summary = item.get("summary")
    summary_html = f'<p class="news-summary">{esc(summary)}</p>' if summary else ""
    return (
        '<article class="news">'
        f'<h4>{title_html}</h4>'
        '<p class="news-meta">'
        f"{esc(item.get('source'))} ｜ {esc(item.get('published_at'))} ｜ "
        f"{sentiment_badge(item.get('sentiment'))} "
        f"<span class=\"muted\">影响主线：{esc(themes or '未匹配')}</span>"
        "</p>"
        f"{summary_html}"
        "</article>"
    )


def pick_items(items: list[dict], category: str, urls: list[str], limit: int) -> list[dict]:
    pool = [item for item in items if item["category"] == category]
    by_url = {item["url"]: item for item in pool}
    chosen = [by_url[url] for url in urls if url in by_url]
    if not chosen:
        pool.sort(key=lambda item: item.get("published_at", ""), reverse=True)
        chosen = pool
    return chosen[:limit]


def render_direction_card(
    label: str,
    row: dict | None,
    basis: str | None,
    switch_meta: dict | None,
    theme_names: dict[str, str],
) -> str:
    if row is None:
        return (
            f'<section class="card direction"><h3>{esc(label)}</h3>'
            '<p class="empty">无符合条件的主线</p></section>'
        )
    switch_html = ""
    if switch_meta and switch_meta.get("new") == row["theme_id"]:
        old_name = theme_names.get(switch_meta.get("old"), switch_meta.get("old"))
        confirm_days = int(switch_meta.get("confirm_days", 2))
        verdict = (
            f"连续{confirm_days}日成立，已确认"
            if switch_meta.get("confirmed")
            else "首日成立，待确认"
        )
        switch_html = f'<p class="switch-note">切换对象：{esc(old_name)}；{esc(verdict)}</p>'
    basis_html = (
        f'<p class="basis">选取依据：{esc(BASIS_LABELS.get(basis, basis))}</p>'
        if basis
        else ""
    )
    return (
        '<section class="card direction">'
        f'<div class="card-head"><h3>{esc(label)}</h3>{status_badge(row.get("status"))}</div>'
        f'<h4 class="theme-name">{esc(row.get("name"))}</h4>'
        '<div class="metrics">'
        f'<span>RS <b>{pct(row.get("rs"))}</b></span>'
        f'<span>量能 <b>{num(row.get("volume_ratio"))}倍</b></span>'
        f'<span>龙头宽度 <b>{pct(row.get("breadth"), 0)}</b></span>'
        f'<span>催化 <b>{esc(row.get("catalyst_count"))} 条</b></span>'
        f'<span>涨停 <b>{esc(row.get("limit_up_count") if row.get("limit_up_count") is not None else "—")}'
        f'/{esc(row.get("limit_up_trend") or "—")}</b></span>'
        f'<span>载体 <b>{esc(row.get("carrier"))}</b></span>'
        "</div>"
        f'<p class="conclusion">{esc(conclusion(row, row.get("status")))}</p>'
        f"{switch_html}{basis_html}"
        "</section>"
    )


def render_all_themes(themes: list[dict]) -> str:
    rows = []
    for row in themes:
        try:
            extras = json.loads(row["extras_json"]) if row.get("extras_json") else {}
        except (TypeError, ValueError):
            extras = {}
        trend_bits = ["站上20日线" if row["trend_above_ma20"] else "跌破20日线"]
        if row["trend_golden_cross"]:
            trend_bits.append("5/10金叉")
        elif row["trend_ma5_over_ma10"]:
            trend_bits.append("5日线>10日线")
        else:
            trend_bits.append("5日线<10日线")
        rows.append(
            [
                esc(row["name"]),
                esc(TIER_LABELS.get(row["tier"], row["tier"])),
                status_badge(row["status"]),
                pct(row["rs"]),
                num(row["volume_ratio"]),
                pct(row["breadth"], 0),
                f"{esc(row['breadth_covered'])}/{esc(extras.get('leaders_total', '—'))}",
                esc(row["catalyst_count"]),
                esc(row["limit_up_count"] if row["limit_up_count"] is not None else "—"),
                pct(row["ret_5d"]),
                esc("；".join(trend_bits)),
                esc(row["carrier"]),
                esc(row["note"] or "—"),
            ]
        )
    return table(
        ["主线", "池", "状态", "RS", "量能倍数", "龙头宽度", "龙头覆盖", "催化(5日)",
         "涨停(家)", "5日涨幅", "趋势", "载体", "备注"],
        rows,
    )


def render_limit_up(rows: list[dict], theme_names: dict[str, str]) -> str:
    data = []
    for row in rows:
        try:
            series = json.loads(row["series_json"]) if row.get("series_json") else []
        except (TypeError, ValueError):
            series = []
        names = "、".join(theme_names.get(tid, tid) for tid in split_themes(row["theme_ids"]))
        data.append(
            [
                esc(row["sector"]),
                esc(row["count"]),
                esc(row["lianban"]),
                esc(row["first_board"]),
                esc(" → ".join("—" if value is None else str(value) for value in series)),
                esc(row["trend"]),
                esc(f"是（{names}）" if row["in_theme_pool"] else "否"),
                esc(row["note"] or "—"),
            ]
        )
    return table(
        ["细分方向", "今日涨停家数", "连板家数", "首板家数", "近5日序列", "近5日趋势",
         "是否属于主线池", "备注"],
        data,
        cls="limit-table",
    )


def render_holdings(rows: list[dict]) -> str:
    data = [
        [
            esc(row["code"]),
            esc(row["name"]),
            esc(row["theme_name"] or "—"),
            status_badge(row["theme_status"]) if row["theme_status"] else "—",
            esc(row["risk_note"] or "—"),
            esc(row["mapping_status"]),
        ]
        for row in rows
    ]
    return table(["代码", "名称", "对应主线", "该主线今日状态", "风险备注", "映射状态"], data)


# ---------------------------------------------------------------- 主页面


def build_page(
    data: dict[str, Any],
    dates: list[str],
    theme_names: dict[str, str],
    config: dict[str, Any],
    is_archive: bool,
) -> str:
    payload = data.get("payload") or {}
    themes = data.get("themes") or []
    news_items = data.get("news") or []
    quotes = data.get("quotes") or []
    limit_rows = data.get("limit_up") or []
    holdings = data.get("holdings") or []
    selected = data["date"]
    theme_by_id = {row["theme_id"]: row for row in themes}
    directions = payload.get("directions") or {}
    switch_meta = payload.get("switch")
    basis = directions.get("basis") or {}

    include_holdings = bool(config.get("publish", {}).get("include_holdings", True))
    refresh_seconds = int(config.get("publish", {}).get("auto_refresh_seconds", 600))
    site_title = str(config.get("publish", {}).get("site_title", "市场主线切换盘后看板"))

    display = payload.get("display") or {}
    policy_items = pick_items(
        news_items, "policy", display.get("policy", []), int(config.get("display_max_policy", 3))
    )
    research_items = pick_items(
        news_items, "research", display.get("research", []), int(config.get("display_max_research", 3))
    )
    policy_html = "".join(render_news(item, theme_names) for item in policy_items) or (
        '<p class="empty">今日无新增政策条目（该源今日缺失或未更新）</p>'
    )
    research_html = "".join(render_news(item, theme_names) for item in research_items) or (
        '<p class="empty">今日无新增研报条目（该源今日缺失或未更新）</p>'
    )

    catalyst_today = payload.get("catalyst_today") or {}
    catalyst_5d = payload.get("catalyst_5d") or {}
    today_chips = "".join(
        f'<span class="chip">{esc(theme_names.get(tid, tid))} <b>{esc(count)}</b></span>'
        for tid, count in catalyst_today.items()
        if count
    ) or '<span class="muted">无命中催化词的新增条目</span>'
    five_day_chips = "".join(
        f'<span class="chip">{esc(theme_names.get(tid, tid))} <b>{esc(count)}</b></span>'
        for tid, count in catalyst_5d.items()
        if count
    )

    quote_rows = [
        [
            esc(row["name"]),
            quote_latest(row["symbol"], row["latest"]),
            quote_change(row["symbol"], row["change_pct"]),
            esc(row["quote_time"] or "—"),
            esc(row["note"] or "—"),
        ]
        for row in quotes
    ]
    quotes_html = (
        table(["品种", "最新", "涨跌幅", "行情时间", "对A股主线影响"], quote_rows)
        if quotes
        else '<p class="empty">该源今日缺失：未获取到美股期货/美元/美债/商品/汇率行情。</p>'
    )

    switch_note = ""
    if switch_meta:
        new_name = theme_names.get(switch_meta.get("new"), switch_meta.get("new"))
        old_name = theme_names.get(switch_meta.get("old"), switch_meta.get("old"))
        switch_note = (
            f'<p class="switch-note">切换观察：{esc(new_name)} ← {esc(old_name)}'
            f"（{'已确认' if switch_meta.get('confirmed') else '待确认'}）</p>"
        )

    card_html = "".join(
        [
            render_direction_card("当前最强主线", theme_by_id.get(directions.get("strongest")),
                                  basis.get("strongest"), switch_meta, theme_names),
            render_direction_card("潜在切换方向", theme_by_id.get(directions.get("switching")),
                                  basis.get("switching"), switch_meta, theme_names),
            render_direction_card("走弱/风险方向", theme_by_id.get(directions.get("weakening")),
                                  basis.get("weakening"), switch_meta, theme_names),
        ]
    )

    radar = payload.get("radar") or []
    radar_rows = [
        [
            esc(row["name"]),
            status_badge(row["prev"]),
            status_badge(row["curr"]),
            esc(row["change_type"]),
            esc(row["detail"]),
        ]
        for row in radar
    ]
    if radar:
        radar_html = table(["主线", "昨日状态", "今日状态", "类型", "说明"], radar_rows)
    elif payload.get("prev_date"):
        radar_html = '<p class="empty">今日无状态变化。</p>'
    else:
        radar_html = '<p class="empty">首次快照，暂无可比昨日状态。</p>'

    failures = payload.get("failed_sources") or []
    failure_html = (
        '<div class="alert">今日缺失/降级信源：' + esc("；".join(failures)) + "</div>"
        if failures
        else ""
    )

    baseline_last = str(payload.get("baseline_last_date") or selected)
    premarket_html = ""
    if baseline_last < selected:
        premarket_html = (
            f'<div class="notice">盘前/盘中快照：行情尚未更新到 {esc(selected)}，'
            f"当前指标基于 {esc(baseline_last)} 收盘数据，涨停热度同为该日。</div>"
        )

    latest = dates[0] if dates else selected
    mode = "最新快照" if selected == latest and not is_archive else "历史快照"
    title_prefix = "" if selected == latest and not is_archive else f"{selected} · "
    page_title = f"{title_prefix}{site_title}"

    holdings_section = ""
    if include_holdings:
        holdings_section = f"""
<section id="d">
  <h2><span class="letter">D</span> 持仓备注 <small>纯展示，不参与计算</small></h2>
  {render_holdings(holdings) if holdings else '<p class="empty">holdings.yaml 未配置持仓。</p>'}
</section>"""

    refresh_meta = (
        f'<meta http-equiv="refresh" content="{refresh_seconds}">'
        if not is_archive and refresh_seconds > 0
        else ""
    )
    refresh_text = (
        f"每 {max(refresh_seconds // 60, 1)} 分钟自动重载"
        if not is_archive and refresh_seconds > 0
        else "历史归档"
    )

    generated_at = data.get("generated_at") or payload.get("generated_at") or "—"
    baseline = payload.get("baseline_index") or "—"

    # 副标题按实际渲染的区块数生成，避免关掉 D 区块后仍写“四区块/持仓备注”
    block_names = ["政策研报", "美股期货", "三大方向"]
    if include_holdings:
        block_names.append("持仓备注")
    sub_label = f"{'四' if include_holdings else '三'}区块：" + " · ".join(block_names)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
{refresh_meta}
<title>{esc(page_title)}</title>
<style>
:root {{
  --bg: #f4f6fb; --card: #ffffff; --text: #16202f; --muted: #6b7788;
  --line: #e3e8f0; --accent: #2457d6; --accent-soft: #eaf0ff;
  --green: #0f8a5f; --green-soft: #e3f6ee;
  --red: #c43b3b; --red-soft: #fdeaea;
  --orange: #b06a00; --orange-soft: #fff3dc;
  --purple: #6b3fa0; --purple-soft: #f0e9fb;
  --gray: #657184; --gray-soft: #eef1f5;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0f141c; --card: #17202c; --text: #e9eef7; --muted: #95a3b8;
    --line: #263242; --accent: #7aa2ff; --accent-soft: #1c2b4a;
    --green: #64d3a4; --green-soft: #123328;
    --red: #ff8b8b; --red-soft: #3a1c1f;
    --orange: #f0b95f; --orange-soft: #362913;
    --purple: #c3a5f7; --purple-soft: #2a2140;
    --gray: #a1adbd; --gray-soft: #232c38;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 22px 16px 40px; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei",
    "PingFang SC", "Noto Sans CJK SC", sans-serif;
  font-size: 14px; line-height: 1.6;
}}
.wrap {{ max-width: 1180px; margin: 0 auto; }}
.hero {{
  display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between;
  gap: 18px; padding: 24px 26px; border-radius: 18px; color: #fff;
  background: linear-gradient(120deg, #173a8a 0%, #2457d6 55%, #3c7af0 100%);
  box-shadow: 0 14px 34px rgba(24, 56, 130, .18);
}}
.eyebrow {{ margin: 0 0 4px; font-size: 11px; letter-spacing: .16em; opacity: .72; }}
.hero h1 {{ margin: 0; font-size: clamp(22px, 3vw, 30px); letter-spacing: .02em; }}
.hero .sub {{ margin: 8px 0 0; font-size: 12.5px; opacity: .8; }}
.hero-side {{ display: flex; flex-direction: column; align-items: flex-end; gap: 8px; }}
.pill {{
  display: inline-block; padding: 3px 12px; border-radius: 999px; font-size: 12px;
  background: rgba(255,255,255,.18); border: 1px solid rgba(255,255,255,.32);
}}
.date-select {{
  min-width: 180px; padding: 7px 10px; border-radius: 9px; font-size: 13px;
  border: 1px solid rgba(255,255,255,.4); color: #fff; background: rgba(10,25,60,.35);
}}
.date-select option {{ color: #16202f; background: #fff; }}
.meta-strip {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px;
  margin: 14px 0 0;
}}
.meta-strip > div {{
  padding: 11px 14px; border-radius: 12px; background: var(--card);
  border: 1px solid var(--line);
}}
.meta-strip span {{ display: block; font-size: 11px; color: var(--muted); letter-spacing: .04em; }}
.meta-strip strong {{ font-size: 13px; font-weight: 600; }}
.toc {{
  display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 4px; font-size: 12px;
}}
.toc a {{
  padding: 5px 12px; border-radius: 999px; text-decoration: none;
  color: var(--accent); background: var(--accent-soft); border: 1px solid transparent;
}}
.toc a:hover {{ border-color: var(--accent); }}
.alert {{
  margin: 12px 0 0; padding: 11px 14px; border-radius: 12px; font-size: 12.5px;
  color: var(--red); background: var(--red-soft); border: 1px solid color-mix(in srgb, var(--red) 30%, transparent);
}}
.notice {{
  margin: 12px 0 0; padding: 11px 14px; border-radius: 12px; font-size: 12.5px;
  color: var(--accent); background: var(--accent-soft);
  border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
}}
section {{ margin: 24px 0 0; }}
h2 {{
  display: flex; align-items: center; gap: 9px; margin: 0 0 12px;
  font-size: 18px; letter-spacing: .01em;
}}
h2 small {{ font-size: 11.5px; font-weight: 400; color: var(--muted); }}
.letter {{
  display: inline-grid; place-items: center; width: 27px; height: 27px; border-radius: 8px;
  font-size: 13px; color: var(--accent); background: var(--accent-soft);
}}
.note {{ margin: 0 0 10px; color: var(--muted); font-size: 12.5px; }}
.grid2 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }}
.panel {{
  padding: 16px; border-radius: 14px; background: var(--card); border: 1px solid var(--line);
}}
.panel h3 {{ margin: 0 0 10px; font-size: 14.5px; }}
.news {{
  padding: 12px 13px; margin: 0 0 9px; border-radius: 11px; background: var(--bg);
  border: 1px solid var(--line);
}}
.news:last-child {{ margin-bottom: 0; }}
.news h4 {{ margin: 0; font-size: 13.5px; line-height: 1.45; }}
.news h4 a {{ color: var(--text); text-decoration: none; }}
.news h4 a:hover {{ color: var(--accent); text-decoration: underline; }}
.news-meta {{ margin: 6px 0 0; font-size: 11.5px; color: var(--muted); }}
.news-summary {{ margin: 7px 0 0; font-size: 12.5px; color: var(--muted); }}
.muted {{ color: var(--muted); }}
.tag {{
  display: inline-block; padding: 1px 7px; border-radius: 6px; font-size: 10.5px;
  color: var(--accent); background: var(--accent-soft);
}}
.badge {{
  display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 11px;
  font-weight: 600; white-space: nowrap;
}}
.s-strengthen {{ color: var(--green); background: var(--green-soft); }}
.s-crowded {{ color: var(--orange); background: var(--orange-soft); }}
.s-weaken {{ color: var(--red); background: var(--red-soft); }}
.s-switch_candidate {{ color: var(--accent); background: var(--accent-soft); }}
.s-switch_confirmed {{ color: var(--purple); background: var(--purple-soft); }}
.s-observe, .s-none {{ color: var(--gray); background: var(--gray-soft); }}
.chips {{ display: flex; flex-wrap: wrap; gap: 7px; margin: 0 0 10px; }}
.chip {{
  padding: 3px 9px; border-radius: 8px; font-size: 11.5px;
  color: var(--muted); background: var(--card); border: 1px solid var(--line);
}}
.chip b {{ color: var(--text); }}
.table-wrap {{ overflow-x: auto; border-radius: 13px; border: 1px solid var(--line); background: var(--card); }}
.limit-table {{ max-height: 460px; overflow: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
th, td {{ padding: 9px 11px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }}
th {{ font-size: 11.5px; font-weight: 600; color: var(--muted); background: var(--bg); white-space: nowrap; }}
tbody tr:nth-child(even) {{ background: color-mix(in srgb, var(--bg) 55%, transparent); }}
tbody tr:last-child td {{ border-bottom: 0; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; }}
.card {{
  padding: 17px; border-radius: 15px; background: var(--card); border: 1px solid var(--line);
}}
.card-head {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
.card-head h3 {{ margin: 0; font-size: 14px; }}
.theme-name {{ margin: 10px 0 8px; font-size: 19px; }}
.metrics {{ display: flex; flex-wrap: wrap; gap: 6px 9px; font-size: 11.5px; color: var(--muted); }}
.metrics span {{ padding: 3px 8px; border-radius: 7px; background: var(--bg); }}
.metrics b {{ color: var(--text); font-weight: 600; }}
.conclusion {{ margin: 10px 0 0; font-size: 12.5px; color: var(--muted); }}
.basis {{ margin: 6px 0 0; font-size: 11px; color: var(--muted); }}
.switch-note {{ margin: 8px 0 0; font-size: 12px; color: var(--orange); }}
details {{
  margin-top: 12px; padding: 14px 16px; border-radius: 14px;
  background: var(--card); border: 1px solid var(--line);
}}
summary {{ cursor: pointer; font-size: 13px; font-weight: 600; }}
details .table-wrap {{ margin-top: 12px; }}
.empty {{
  margin: 0; padding: 14px; border-radius: 10px; font-size: 12.5px;
  color: var(--muted); background: var(--bg); border: 1px dashed var(--line);
}}
footer {{
  margin-top: 30px; padding-top: 16px; border-top: 1px solid var(--line);
  font-size: 11.5px; color: var(--muted);
}}
footer p {{ margin: 4px 0; }}
@media (max-width: 640px) {{
  body {{ padding: 12px 10px 28px; }}
  .hero {{ padding: 19px 18px; }}
  .hero-side {{ align-items: flex-start; }}
  .metrics span {{ font-size: 11px; }}
}}
</style>
</head>
<body>
<div class="wrap">

<header class="hero">
  <div>
    <p class="eyebrow">MARKET MAINLINE DASHBOARD</p>
    <h1>{esc(site_title)}</h1>
    <p class="sub">交易时段每小时刷新 ｜ {sub_label}</p>
  </div>
  <div class="hero-side">
    <span class="pill">{mode}</span>
    {render_date_switcher(dates, selected, is_archive)}
  </div>
</header>

<div class="meta-strip">
  <div><span>快照日期</span><strong>{esc(date_label(selected))}</strong></div>
  <div><span>生成时间（北京时间）</span><strong>{esc(generated_at)}</strong></div>
  <div><span>行情截至</span><strong>{esc(baseline_last)}</strong></div>
  <div><span>基准指数</span><strong>{esc(baseline)}</strong></div>
  <div><span>页面刷新</span><strong>{esc(refresh_text)}</strong></div>
</div>

<nav class="toc">
  <a href="#a">A 政策与研报</a><a href="#b">B 美股期货</a>
  <a href="#c">C 三大方向</a>{'<a href="#d">D 持仓备注</a>' if include_holdings else ''}
</nav>

{failure_html}
{premarket_html}

<section id="a">
  <h2><span class="letter">A</span> 公开政策与研报</h2>
  <p class="note">政策与研报仅保留公开链接，不抓取付费全文。</p>
  <div class="grid2">
    <div class="panel"><h3>政策（最多3条）</h3>{policy_html}</div>
    <div class="panel"><h3>研报（最多3条）</h3>{research_html}</div>
  </div>
  <p class="note" style="margin-top:12px">今日催化计数</p>
  <div class="chips">{today_chips}</div>
  <p class="note">近5日催化</p>
  <div class="chips">{five_day_chips or '<span class="muted">暂无</span>'}</div>
</section>

<section id="b">
  <h2><span class="letter">B</span> 美股期货走势</h2>
  <p class="note">仅描述外围环境，不构成对A股涨跌的预测。</p>
  {quotes_html}
</section>

<section id="c">
  <h2><span class="letter">C</span> 三大方向</h2>
  {switch_note}
  <div class="cards">{card_html}</div>
  <h3 style="margin:20px 0 8px;font-size:14px">切换雷达（仅列状态变化）</h3>
  {radar_html}
  <h3 style="margin:20px 0 8px;font-size:14px">涨停方向热度（仅作情绪验证）</h3>
  {render_limit_up(limit_rows, theme_names) if limit_rows else '<p class="empty">涨停源今日缺失，或当日无涨停数据。</p>'}
  <details>
    <summary>全部主线指标与状态</summary>
    {render_all_themes(themes)}
  </details>
</section>

{holdings_section}

<footer>
  <p>数据来源：中国政府网、央行、证监会、统计局、东方财富研报、华尔街见闻、腾讯/新浪行情、东方财富涨停池。</p>
  <p>本看板仅用于研究与信息整理，不构成任何投资建议；不对涨跌做预测。</p>
  <p>页面由 <code>publish.py</code> 从本地快照生成；数据刷新与推送由 Windows 计划任务在交易时段每小时执行。</p>
</footer>

</div>
</body>
</html>
"""


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="导出静态 HTML 看板")
    parser.add_argument("--date", help="只重新生成指定日期")
    args = parser.parse_args()

    config = load_config()
    themes = load_themes(config)
    theme_names = {theme.id: theme.name for theme in themes}

    db_path = Path(config["output"]["sqlite_path"])
    if not db_path.exists():
        print(f"数据库不存在：{db_path}，请先运行 run_daily.py")
        return 1

    publish_cfg = config.get("publish") or {}
    output_dir = Path(publish_cfg.get("output_dir", "docs"))
    if not output_dir.is_absolute():
        output_dir = Path(config["_root"]) / output_dir
    archive_limit = int(publish_cfg.get("archive_limit", 180))

    conn = db.connect(db_path)
    try:
        dates = db.list_dates(conn)[:archive_limit]
        if not dates:
            print("数据库中还没有任何快照。")
            return 1

        targets = [args.date] if args.date else dates
        (output_dir / "date").mkdir(parents=True, exist_ok=True)

        generated = 0
        for date_str in targets:
            data = db.load_day(conn, date_str)
            if not data:
                print(f"跳过 {date_str}：快照缺失")
                continue
            page = build_page(
                data,
                dates,
                theme_names,
                config,
                is_archive=True,
            )
            path = output_dir / "date" / f"{date_str}.html"
            path.write_text(page, encoding="utf-8")
            generated += 1

        latest_page = build_page(
            db.load_day(conn, dates[0]),
            dates,
            theme_names,
            config,
            is_archive=False,
        )
        (output_dir / "index.html").write_text(latest_page, encoding="utf-8")
        (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    finally:
        conn.close()

    keep = set(dates)
    for path in (output_dir / "date").glob("*.html"):
        if path.stem not in keep:
            path.unlink()

    print(f"已生成 {generated + 1} 个页面：{output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

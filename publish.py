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
from datetime import date, timedelta
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


def pct_signless(value: float | None, digits: int = 0) -> str:
    """不带正负号的百分比（用于龙头宽度 0%/60%/100%）。"""
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"


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


def render_date_switcher(
    dates: list[str], selected: str, is_archive: bool, trade_days: list[str] | None = None
) -> str:
    if not dates:
        return ""
    snapshot_set = set(dates)
    trade_set = set(trade_days or [])
    start = date.fromisoformat(dates[-1])
    end = date.fromisoformat(dates[0])
    full: list[str] = []
    day = start
    while day <= end:
        full.append(day.isoformat())
        day += timedelta(days=1)

    def _gap_label(iso: str) -> str:
        d = date.fromisoformat(iso)
        if d.weekday() >= 5:
            return "非交易日"
        if iso in trade_set:
            return "数据缺失"
        return "非交易日"

    options = []
    for item in full:
        if item in snapshot_set:
            if item == dates[0]:
                value = "../index.html" if is_archive else "index.html"
                label = f"最新 · {item}"
            else:
                value = f"{item}.html" if is_archive else f"date/{item}.html"
                label = date_label(item)
            disabled = ""
        else:
            value = ""
            label = f"{item} · {_gap_label(item)}"
            disabled = " disabled"
        current = " selected" if item == selected else ""
        options.append(f'<option value="{value}"{current}{disabled}>{esc(label)}</option>')
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
    pool.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    by_url = {item["url"]: item for item in pool}
    chosen = [by_url[url] for url in urls if url in by_url]
    chosen_urls = {item["url"] for item in chosen}
    for item in pool:
        if len(chosen) >= limit:
            break
        if item["url"] in chosen_urls:
            continue
        chosen.append(item)
        chosen_urls.add(item["url"])
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
    fb_badge = (
        '<span class="badge s-none">兜底</span>'
        if basis and str(basis).startswith("rs_")
        else ""
    )
    return (
        '<section class="card direction">'
        f'<div class="card-head"><h3>{esc(label)}</h3>{status_badge(row.get("status"))}{fb_badge}</div>'
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
                pct_signless(row["breadth"]),
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


def render_limit_up(rows: list[dict], theme_names: dict[str, str], config: dict[str, Any]) -> str:
    min_count = int(config.get("limit_up", {}).get("min_count_per_day", 3))
    filter_default = bool(config.get("limit_up", {}).get("filter_default", True))
    headers = ["细分方向", "今日涨停家数", "连板家数", "首板家数", "近5日序列", "近5日趋势",
               "是否属于主线池", "备注"]
    body_rows = []
    for row in rows:
        try:
            series = json.loads(row["series_json"]) if row.get("series_json") else []
        except (TypeError, ValueError):
            series = []
        in_pool = bool(row["in_theme_pool"])
        tail = [v for v in series[-2:] if v is not None]
        is_candidate = (not in_pool) and len(tail) == 2 and all(v >= min_count for v in tail)
        extra = "" if (in_pool or is_candidate) else ' class="filter-hidden" data-extra="1"'
        names = "、".join(theme_names.get(tid, tid) for tid in split_themes(row["theme_ids"]))
        body_rows.append(
            f"<tr{extra}>"
            f"<td>{esc(row['sector'])}</td>"
            f"<td>{esc(row['count'])}</td>"
            f"<td>{esc(row['lianban'])}</td>"
            f"<td>{esc(row['first_board'])}</td>"
            f"<td>{esc(' → '.join('—' if v is None else str(v) for v in series))}</td>"
            f"<td>{esc(row['trend'])}</td>"
            f"<td>{esc(f'是（{names}）' if in_pool else '否')}</td>"
            f"<td>{esc(row['note'] or '—')}</td>"
            "</tr>"
        )
    if not body_rows:
        return '<p class="empty">涨停源今日缺失，或当日无涨停数据。</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    toolbar = (
        '<div class="limit-toolbar">'
        '<label><input type="checkbox" id="lu-show-all"'
        f"{'' if filter_default else ' checked'}> 显示全部（含非主线池方向）</label>"
        "</div>"
    )
    return (
        toolbar
        + '<div class="table-wrap limit-table"><table id="lu-table"><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
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


def _dedupe_news(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        key = item.get("event_key") or item.get("url") or item.get("title")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def render_today_bishop(
    items: list[dict], theme_names: dict[str, str], config: dict[str, Any]
) -> str:
    cfg = config.get("news") or {}
    limit = int(cfg.get("today_bishop_limit", 3))
    max_per_theme = int(cfg.get("today_bishop_max_per_theme", 2))
    pool = [
        item for item in items
        if item.get("matched_themes")
        and item.get("category") in ("policy", "research", "international")
    ]
    if not pool:
        return '<p class="empty">今日暂无命中主线的政策/研报/国际要闻。</p>'
    order = {"likely_positive": 0, "negative": 0, "uncertain": 1, "neutral": 2}
    pool.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    pool.sort(key=lambda item: (
        -int(item.get("source_weight") or 1),
        order.get(item.get("sentiment"), 2),
    ))
    chosen: list[dict] = []
    used: dict[str, int] = {}
    for item in pool:
        if len(chosen) >= limit:
            break
        primary = (item.get("matched_themes") or "").split(",")[0]
        if used.get(primary, 0) >= max_per_theme:
            continue
        chosen.append(item)
        used[primary] = used.get(primary, 0) + 1
    return "".join(render_news(item, theme_names) for item in chosen)


def render_international(
    items: list[dict], theme_names: dict[str, str], config: dict[str, Any]
) -> str:
    cfg = config.get("news") or {}
    limit = int(cfg.get("international_limit", 5))
    pool = [item for item in items if item.get("category") == "international"]
    pool.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    if not pool:
        return '<p class="empty">今日无命中催化词的国际要闻。</p>'
    return "".join(render_news(item, theme_names) for item in pool[:limit])


def render_news_feed(
    items: list[dict], theme_names: dict[str, str], config: dict[str, Any]
) -> str:
    cfg = config.get("news") or {}
    limit = int(cfg.get("news_display_max", 10))
    pool = [item for item in items if item.get("category") == "news"]
    pool.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    pool = _dedupe_news(pool)
    if not pool:
        return '<p class="empty">今日暂无命中主线的实时快讯。</p>'
    return "".join(render_news(item, theme_names) for item in pool[:limit])


def render_catalyst(
    themes: list[dict],
    theme_names: dict[str, str],
    catalyst_today: dict[str, int],
    catalyst_5d: dict[str, int],
    news_items: list[dict],
    selected: str,
    window_dates: list[str],
) -> tuple[str, str]:
    today_days = {selected}
    five_days = set(window_dates) if window_dates else {selected}

    def _group(theme_id: str, days: set[str]) -> list[dict]:
        return [
            item for item in news_items
            if theme_id in split_themes(item.get("matched_themes"))
            and item.get("published_at", "")[:10] in days
        ]

    def _chips(counts: dict[str, int], days: set[str], prefix: str) -> str:
        parts: list[str] = []
        for idx, theme in enumerate(themes):
            theme_id = theme["theme_id"]
            count = counts.get(theme_id, 0)
            detail_id = f"{prefix}-{idx}"
            parts.append(
                f'<span class="chip chip-toggle" data-target="{detail_id}">'
                f"{esc(theme_names.get(theme_id, theme_id))} <b>{esc(count)}</b></span>"
            )
            detail = "".join(
                render_news(item, theme_names) for item in _dedupe_news(_group(theme_id, days))
            )
            empty_html = '<p class="empty">该主线无命中条目。</p>'
            parts.append(
                f'<div class="chip-detail" id="{detail_id}">'
                f"{detail or empty_html}"
                "</div>"
            )
        return "".join(parts)

    return (
        _chips(catalyst_today, today_days, "cat-t"),
        _chips(catalyst_5d, five_days, "cat-f"),
    )


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
    default_tab = str(config.get("publish", {}).get("default_tab", "mainline"))
    if default_tab not in ("news", "mainline"):
        default_tab = "mainline"

    display = payload.get("display") or {}
    policy_items = pick_items(
        news_items, "policy", display.get("policy", []), int(config.get("display_max_policy", 3))
    )
    research_items = pick_items(
        news_items, "research", display.get("research", []), int(config.get("display_max_research", 3))
    )
    policy_html = "".join(render_news(item, theme_names) for item in policy_items) or (
        '<p class="empty">今日无命中政策（无政策条目命中主线催化词）</p>'
    )
    research_html = "".join(render_news(item, theme_names) for item in research_items) or (
        '<p class="empty">今日无命中研报（无研报在标题+摘要+行业标签至少两维命中主线）</p>'
    )
    bishop_html = render_today_bishop(news_items, theme_names, config)
    international_html = render_international(news_items, theme_names, config)
    news_feed_html = render_news_feed(news_items, theme_names, config)

    catalyst_today = payload.get("catalyst_today") or {}
    catalyst_5d = payload.get("catalyst_5d") or {}
    catalyst_today_html, catalyst_5d_html = render_catalyst(
        themes, theme_names, catalyst_today, catalyst_5d, news_items,
        selected, payload.get("window_dates") or [],
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
    any_status_dir = any(
        str(basis.get(key)) == "status" for key in ("strongest", "switching", "weakening")
    )
    directions_fallback_html = (
        ""
        if any_status_dir
        else '<div class="notice">今日无主线触发强化/走弱/拥挤，以下三大方向为相对排名兜底。</div>'
    )

    radar = payload.get("radar") or []
    radar_rows = [
        [
            esc(row["name"]),
            status_badge(row["prev"]),
            status_badge(row["curr"]),
            esc(row["change_type"]),
            esc(row.get("reason") or row.get("detail") or "—"),
            esc(row["detail"]),
        ]
        for row in radar
    ]
    if radar:
        radar_html = table(["主线", "昨日状态", "今日状态", "类型", "变化原因", "说明"], radar_rows)
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
    stale_html = ""
    if baseline_last < selected:
        stale_html = (
            f'<div class="stale-alert">行情未更新到当日，指标基于 {esc(baseline_last)} 收盘数据。</div>'
        )

    latest = dates[0] if dates else selected
    mode = "最新快照" if selected == latest and not is_archive else "历史快照"
    title_prefix = "" if selected == latest and not is_archive else f"{selected} · "
    page_title = f"{title_prefix}{site_title}"

    holdings_section = ""
    if include_holdings:
        holdings_section = f"""
  <h2>持仓备注 <small>纯展示，不参与计算</small></h2>
  {render_holdings(holdings) if holdings else '<p class="empty">holdings.yaml 未配置持仓。</p>'}"""

    refresh_meta = (
        f'<meta http-equiv="refresh" content="{refresh_seconds}">'
        if not is_archive and refresh_seconds > 0
        else ""
    )
    minutes = max(refresh_seconds // 60, 1)
    refresh_note_template = str(config.get("publish", {}).get(
        "refresh_data_note", "页面每{minutes}分钟自动重载；数据由计划任务定时更新"))
    refresh_text = (
        refresh_note_template.replace("{minutes}", str(minutes))
        if not is_archive and refresh_seconds > 0
        else "历史归档"
    )

    generated_at = data.get("generated_at") or payload.get("generated_at") or "—"
    baseline = payload.get("baseline_index") or "—"

    sub_label = "两个 Tab：新闻与催化 ｜ 主线与方向"

    limit_up_html = (
        render_limit_up(limit_rows, theme_names, config)
        if limit_rows
        else '<p class="empty">涨停源今日缺失，或当日无涨停数据。</p>'
    )

    page_script = (
        "(function(){\n"
        "  var tabs=document.querySelectorAll('.tab');\n"
        "  function switchTab(name){\n"
        "    for(var i=0;i<tabs.length;i++){tabs[i].classList.toggle('active',tabs[i].getAttribute('data-tab')===name);}\n"
        "    var panes=document.querySelectorAll('.tabpane');\n"
        "    for(var j=0;j<panes.length;j++){panes[j].classList.toggle('active',panes[j].id==='tab-'+name);}\n"
        "    if(location.hash!=='#'+name){history.replaceState(null,'','#'+name);}\n"
        "  }\n"
        "  function initTab(){\n"
        "    var h=location.hash.replace('#','');\n"
        "    var v=(h==='news'||h==='mainline');\n"
        "    switchTab(v?h:'__DEFAULT__');\n"
        "  }\n"
        "  for(var i=0;i<tabs.length;i++){\n"
        "    tabs[i].addEventListener('click',function(){switchTab(this.getAttribute('data-tab'));});\n"
        "  }\n"
        "  window.addEventListener('hashchange',function(){\n"
        "    var h=location.hash.replace('#','');\n"
        "    if(h==='news'||h==='mainline'){switchTab(h);}\n"
        "  });\n"
        "  initTab();\n"
        "  var lu=document.getElementById('lu-show-all');\n"
        "  function applyLu(){\n"
        "    if(!lu){return;}\n"
        "    var rows=document.querySelectorAll('#lu-table tbody tr[data-extra=\"1\"]');\n"
        "    for(var i=0;i<rows.length;i++){rows[i].classList.toggle('filter-hidden',!lu.checked);}\n"
        "  }\n"
        "  if(lu){lu.addEventListener('change',applyLu);applyLu();}\n"
        "  document.addEventListener('click',function(e){\n"
        "    var t=e.target.closest?e.target.closest('.chip-toggle'):null;\n"
        "    if(!t){return;}\n"
        "    var d=document.getElementById(t.getAttribute('data-target'));\n"
        "    if(d){d.classList.toggle('open');}\n"
        "  });\n"
        "})();"
    ).replace("__DEFAULT__", default_tab)

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
.tabbar {{
  display: flex; gap: 8px; margin: 14px 0 4px; padding-bottom: 2px;
  overflow-x: auto; -webkit-overflow-scrolling: touch; border-bottom: 2px solid var(--line);
}}
.tab {{
  flex: 0 0 auto; padding: 9px 16px; border: none; background: transparent; cursor: pointer;
  font-size: 14px; font-weight: 600; color: var(--muted); white-space: nowrap;
  border-bottom: 2px solid transparent; margin-bottom: -2px;
}}
.tab:hover {{ color: var(--accent); }}
.tab.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
.tabpane {{ display: none; }}
.tabpane.active {{ display: block; }}
.stale-alert {{
  margin: 12px 0 0; padding: 11px 14px; border-radius: 12px; font-size: 12.5px;
  color: var(--red); background: var(--red-soft);
  border: 1px solid color-mix(in srgb, var(--red) 30%, transparent);
}}
.chip-toggle {{ cursor: pointer; user-select: none; }}
.chip-toggle:hover {{ border-color: var(--accent); color: var(--accent); }}
.chip-detail {{ display: none; width: 100%; margin: 2px 0 10px; }}
.chip-detail.open {{ display: block; }}
.filter-hidden {{ display: none; }}
.limit-toolbar {{ margin: 0 0 10px; font-size: 12.5px; color: var(--muted); }}
.limit-toolbar label {{ cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }}
</style>
</head>
<body>
<div class="wrap">

<header class="hero">
  <div>
    <p class="eyebrow">MARKET MAINLINE DASHBOARD</p>
    <h1>{esc(site_title)}</h1>
    <p class="sub">{esc(refresh_text)} ｜ {sub_label}</p>
  </div>
  <div class="hero-side">
    <span class="pill">{mode}</span>
    {render_date_switcher(dates, selected, is_archive, payload.get("trade_days"))}
  </div>
</header>

<div class="meta-strip">
  <div><span>快照日期</span><strong>{esc(date_label(selected))}</strong></div>
  <div><span>生成时间（北京时间）</span><strong>{esc(generated_at)}</strong></div>
  <div><span>行情截至</span><strong>{esc(baseline_last)}</strong></div>
  <div><span>基准指数</span><strong>{esc(baseline)}</strong></div>
  <div><span>页面刷新</span><strong>{esc(refresh_text)}</strong></div>
</div>

<nav class="tabbar">
  <button type="button" class="tab" data-tab="mainline">主线与方向</button>
  <button type="button" class="tab" data-tab="news">新闻与催化</button>
</nav>

{failure_html}
{stale_html}

<div class="tabpane active" id="tab-mainline">
  <section>
    <h2><span class="letter">A</span> 美股期货走势</h2>
    <p class="note">仅描述外围环境，不构成对A股涨跌的预测。</p>
    {quotes_html}
  </section>

  <section>
    <h2><span class="letter">B</span> 三大方向</h2>
    {directions_fallback_html}
    {switch_note}
    <div class="cards">{card_html}</div>
    <h3 style="margin:20px 0 8px;font-size:14px">切换雷达（仅列状态变化）</h3>
    {radar_html}
    <details>
      <summary>全部主线指标与状态</summary>
      {render_all_themes(themes)}
    </details>
  </section>

  {holdings_section}
</div>

<div class="tabpane" id="tab-news">
  <section>
    <h2><span class="letter">A</span> 今日必看</h2>
    {bishop_html}
  </section>

  <section>
    <h2><span class="letter">B</span> 公开政策与研报</h2>
    <p class="note">政策与研报仅保留公开链接，不抓取付费全文。</p>
    <div class="grid2">
      <div class="panel"><h3>政策（最多6条）</h3>{policy_html}</div>
      <div class="panel"><h3>研报（最多6条）</h3>{research_html}</div>
    </div>
  </section>

  <section>
    <h2><span class="letter">C</span> 实时快讯</h2>
    <p class="note">财联社电报与华尔街见闻快讯，仅保留命中主线催化词的条目。</p>
    {news_feed_html}
  </section>

  <section>
    <h2><span class="letter">D</span> 隔夜国际要闻</h2>
    {international_html}
  </section>

  <section>
    <h2><span class="letter">E</span> 催化计数</h2>
    <p class="note">同一事件多源只计一次；未命中主线显示 0。点计数可展开该主线条目。</p>
    <p class="note" style="margin-top:4px">今日</p>
    <div class="chips">{catalyst_today_html}</div>
    <p class="note" style="margin-top:8px">近5日</p>
    <div class="chips">{catalyst_5d_html}</div>
  </section>

  <section>
    <h2><span class="letter">F</span> 涨停方向热度</h2>
    <p class="note">仅作情绪验证，不参与核心判定。默认只显示主线池与连续两日候选方向。</p>
    {limit_up_html}
  </section>
</div>

<footer>
  <p>数据来源：中国政府网、央行、证监会、统计局、人民日报、新华社、东方财富研报、华尔街见闻、财联社、腾讯/新浪行情、东方财富涨停池。</p>
  <p>本看板仅用于研究与信息整理，不构成任何投资建议；不对涨跌做预测。</p>
  <p>{esc(refresh_text)}。页面由 <code>publish.py</code> 从本地快照生成。</p>
</footer>

</div>
<script>{page_script}</script>
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

"""公开政策、券商研报与财经快讯采集。

合规约束：
- 只访问公开页面/公开接口，低频请求；
- 研报只取标题、机构、日期、摘要与链接，不抓付费全文；
- 快讯只保留命中主线催化词的条目。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..config import Theme
from ..logging_setup import TZ

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

POS_WORDS = [
    "支持", "鼓励", "促进", "推动", "利好", "增持", "回购", "降准", "降息",
    "超预期", "突破", "获批", "放量", "复苏", "改善", "提升", "扩大",
]
NEG_WORDS = [
    "制裁", "加征", "关税", "处罚", "立案", "下调", "低于预期", "下滑",
    "亏损", "减持", "限制", "禁止", "风险", "承压", "收紧", "冲突",
]


def _get(url: str, timeout: int = 25) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _date_in_window(value: str, start: date, end: date) -> bool:
    try:
        return start <= date.fromisoformat(value[:10]) <= end
    except ValueError:
        return False


def _date_from_url(url: str) -> str | None:
    match = re.search(r"(20\d{2})(\d{2})(\d{2})", url)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def fetch_gov_policy(start: date, end: date) -> list[dict[str, Any]]:
    url = "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"
    data = _get(url).json()
    items = []
    for row in data:
        pub = str(row.get("DOCRELPUBTIME", ""))[:10]
        if not _date_in_window(pub, start, end):
            continue
        items.append(
            {
                "category": "policy",
                "title": _clean(row.get("TITLE", "")),
                "source": "中国政府网",
                "published_at": pub,
                "summary": "",
                "url": row.get("URL", ""),
            }
        )
    return items


def fetch_pboc(start: date, end: date) -> list[dict[str, Any]]:
    url = "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html"
    resp = _get(url)
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in soup.select("a"):
        href = a.get("href") or ""
        title = _clean(a.get_text())
        if len(title) < 8 or "/goutongjiaoliu/113456/113469/" not in href:
            continue
        pub = _date_from_url(href)
        if not pub or not _date_in_window(pub, start, end):
            continue
        full_url = urljoin("http://www.pbc.gov.cn", href)
        if full_url in seen:
            continue
        seen.add(full_url)
        items.append(
            {
                "category": "policy",
                "title": title,
                "source": "中国人民银行",
                "published_at": pub,
                "summary": "",
                "url": full_url,
            }
        )
    return items[:30]


def fetch_csrc(start: date, end: date) -> list[dict[str, Any]]:
    url = "https://www.csrc.gov.cn/csrc/c100028/common_list.shtml"
    resp = _get(url)
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in soup.select("a"):
        href = a.get("href") or ""
        title = _clean(a.get_text())
        if len(title) < 10 or not href.startswith("/csrc/"):
            continue
        parent = a.find_parent(["li", "tr", "div"])
        parent_text = parent.get_text(" ", strip=True) if parent else ""
        match = re.search(r"(20\d{2}-\d{2}-\d{2})", parent_text)
        if not match:
            continue
        pub = match.group(1)
        if not _date_in_window(pub, start, end):
            continue
        full_url = urljoin("https://www.csrc.gov.cn", href)
        if full_url in seen:
            continue
        seen.add(full_url)
        items.append(
            {
                "category": "policy",
                "title": title,
                "source": "中国证监会",
                "published_at": pub,
                "summary": "",
                "url": full_url,
            }
        )
    return items[:30]


def fetch_stats(start: date, end: date) -> list[dict[str, Any]]:
    base = "https://www.stats.gov.cn/sj/zxfb/"
    resp = _get(base)
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in soup.select("a"):
        href = a.get("href") or ""
        title = _clean(a.get_text())
        if "t20" not in href or len(title) < 8:
            continue
        pub = _date_from_url(href)
        if not pub or not _date_in_window(pub, start, end):
            continue
        full_url = urljoin(base, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        items.append(
            {
                "category": "policy",
                "title": title,
                "source": "国家统计局",
                "published_at": pub,
                "summary": "",
                "url": full_url,
            }
        )
    return items[:30]


def fetch_people_cn(start: date, end: date) -> list[dict[str, Any]]:
    """人民日报评论/发声（人民网观点频道：人民锐评/人民时评/评论员等），category=policy。"""
    url = "http://opinion.people.com.cn/"
    resp = _get(url)
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        title = _clean(a.get_text())
        m = re.search(r"/n1/(20\d{2})/(\d{2})(\d{2})/", href)
        if not m or len(title) < 8:
            continue
        pub = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if not _date_in_window(pub, start, end):
            continue
        full_url = urljoin("http://opinion.people.com.cn", href)
        if full_url in seen:
            continue
        seen.add(full_url)
        items.append(
            {
                "category": "policy",
                "title": title,
                "source": "人民日报",
                "published_at": pub,
                "summary": "",
                "url": full_url,
            }
        )
    return items[:30]


_XINHUA_KEEP_WORDS = [
    "发布", "印发", "召开", "政策", "评论", "时评", "观察", "方案", "意见",
    "通知", "规划", "部署", "改革", "会议", "条例", "规定", "讲话", "会见",
    "调研", "签署", "合作", "公布", "通报", "出台", "精神", "清单", "公告",
]


def fetch_xinhua(start: date, end: date) -> list[dict[str, Any]]:
    """新华社时政频道 HTML（权威政策/发声），category=policy。"""
    url = "http://www.news.cn/politics/"
    resp = _get(url)
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        title = _clean(a.get_text())
        if len(title) < 10 or "/comments/" in href or "/zt/" in href:
            continue
        if not _match_words(title, _XINHUA_KEEP_WORDS):
            continue
        pub = _date_from_url(href)
        if not pub or not _date_in_window(pub, start, end):
            continue
        full_url = urljoin("http://www.news.cn", href)
        if full_url in seen:
            continue
        seen.add(full_url)
        items.append(
            {
                "category": "policy",
                "title": title,
                "source": "新华社",
                "published_at": pub,
                "summary": "",
                "url": full_url,
            }
        )
    return items[:30]


def fetch_cls(start: date, end: date) -> list[dict[str, Any]]:
    """财联社电报（实时快讯，覆盖英伟达/美股科技/政策突发），category=news。"""
    import akshare as ak

    df = ak.stock_info_global_cls(symbol="全部")
    items: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        title = _clean(str(row.get("标题", "")))
        content = _clean(str(row.get("内容", "")))
        pub_day = str(row.get("发布日期", ""))[:10]
        pub_time = str(row.get("发布时间", ""))
        if not title or not _date_in_window(pub_day, start, end):
            continue
        published_at = f"{pub_day} {pub_time}" if pub_time and pub_time != "None" else pub_day
        # 财联社电报无独立 URL，用标题+时间哈希构造稳定伪 URL 供去重/事件键使用
        pseudo = hashlib.md5((title + pub_day + pub_time).encode("utf-8")).hexdigest()[:16]
        items.append(
            {
                "category": "news",
                "title": title,
                "source": "财联社",
                "published_at": published_at,
                "summary": content[:220],
                "url": f"cls://{pseudo}",
            }
        )
    return items


def fetch_research(start: date, end: date) -> list[dict[str, Any]]:
    url = "https://reportapi.eastmoney.com/report/list"
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for qtype in (1, 2):
        params = {
            "cb": "cb",
            "pageSize": 50,
            "beginTime": start.isoformat(),
            "endTime": end.isoformat(),
            "pageNo": 1,
            "qType": qtype,
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        text = resp.text
        try:
            payload = json.loads(text[text.index("(") + 1: text.rindex(")")])
        except ValueError:
            continue
        for row in payload.get("data") or []:
            info_code = str(row.get("infoCode", ""))
            if not info_code or info_code in seen:
                continue
            seen.add(info_code)
            pub = str(row.get("publishDate", ""))[:10]
            if not _date_in_window(pub, start, end):
                continue
            rating = _clean(row.get("emRatingName") or row.get("sRatingName") or "")
            industry = _clean(row.get("industryName") or "")
            org = _clean(row.get("orgSName") or row.get("orgName") or "")
            page = "zw_industry" if qtype == 1 else "zw_strategy"
            summary_parts = [part for part in (industry, rating, org) if part]
            items.append(
                {
                    "category": "research",
                    "title": _clean(row.get("title", "")),
                    "source": org or "东方财富研报",
                    "published_at": pub,
                    "summary": "｜".join(summary_parts),
                    "url": f"https://data.eastmoney.com/report/{page}.jshtml?infocode={info_code}",
                    "industry": industry,
                    "rating": rating,
                }
            )
    return items[:60]


WSCN_URL = (
    "https://api-one-wscn.awtmt.com/apiv1/content/lives"
    "?channel=global-channel&client=pc&limit=100"
)


def _fetch_wscn_raw(start: date, end: date) -> list[dict[str, Any]]:
    """拉取华尔街见闻全球快讯原始条目（未定 category），供快讯/国际要闻复用。"""
    payload = _get(WSCN_URL).json()
    items: list[dict[str, Any]] = []
    for row in (payload.get("data") or {}).get("items") or []:
        text = _clean(row.get("content_text", ""))
        if not text:
            continue
        try:
            pub_dt = datetime.fromtimestamp(int(row.get("display_time", 0)), TZ)
        except (ValueError, TypeError):
            continue
        if not (start <= pub_dt.date() <= end):
            continue
        title = _clean(row.get("highlight_title") or text[:60])
        items.append(
            {
                "title": title,
                "source": "华尔街见闻",
                "published_at": pub_dt.strftime("%Y-%m-%d %H:%M"),
                "summary": text[:220],
                "url": row.get("uri") or "",
            }
        )
    return items[:80]


def fetch_wallstreetcn(start: date, end: date) -> list[dict[str, Any]]:
    return [{**item, "category": "news"} for item in _fetch_wscn_raw(start, end)]


def classify_sentiment(text: str) -> str:
    pos = sum(1 for word in POS_WORDS if word in text)
    neg = sum(1 for word in NEG_WORDS if word in text)
    if pos > neg:
        return "likely_positive"
    if neg > pos:
        return "negative"
    if pos == neg == 0:
        return "neutral"
    return "uncertain"


MATCH_NOISE = [
    "中国人民银行", "人民银行", "中国证监会", "国家统计局",
    "中国政府网", "华尔街见闻", "东方财富",
]


def _strip_noise(text: str) -> str:
    for noise in MATCH_NOISE:
        text = text.replace(noise, "")
    return text


def match_themes(text: str, themes: list[Theme]) -> dict[str, int]:
    """按催化词命中数匹配主线；先剔除机构名，避免“人民银行→银行”这类误命中。"""
    text = _strip_noise(text)
    hits: dict[str, int] = {}
    lowered = text.lower()
    for theme in themes:
        count = sum(1 for word in theme.catalyst_words if word.lower() in lowered)
        if count:
            hits[theme.id] = count
    return hits


def _match_words(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


def match_research(
    title: str, summary: str, industry: str, themes: list[Theme], min_dims: int = 2
) -> dict[str, int]:
    """研报三重命中：标题、摘要、行业标签三个维度，至少 min_dims 维命中才算匹配；
    命中值记录为命中维度数，避免单个词误配（如“伊朗战争对电力设备”→半导体）。"""
    hits: dict[str, int] = {}
    fields = [
        _strip_noise(title).lower(),
        _strip_noise(summary).lower(),
        (industry or "").lower(),
    ]
    for theme in themes:
        words = [word.lower() for word in theme.catalyst_words]
        dims = sum(1 for field in fields if any(word in field for word in words))
        if dims >= min_dims:
            hits[theme.id] = dims
    return hits


def _normalize_title(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", title.lower())


def assign_event_keys(items: list[dict[str, Any]]) -> None:
    """同一事件多源只计 1 次：标题相似度 >= 0.82 视为同一事件。"""
    keys: list[tuple[str, str]] = []
    for item in sorted(items, key=lambda x: x.get("published_at", "")):
        norm = _normalize_title(item["title"])
        if not norm:
            item["event_key"] = hashlib.md5(item["url"].encode("utf-8")).hexdigest()[:16]
            continue
        matched = None
        for existing, key in keys:
            if difflib.SequenceMatcher(None, norm, existing).ratio() >= 0.82:
                matched = key
                break
        if matched is None:
            matched = hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]
            keys.append((norm, matched))
        item["event_key"] = matched


ARTICLE_SELECTORS = ["#UCAP-CONTENT", ".pages_content", "#zoom", ".TRS_Editor", ".content"]


def enrich_policy_summaries(items: list[dict[str, Any]], max_items: int = 3) -> None:
    """仅为看板展示的政策条目抓取公开正文首段，失败则保留标题。"""
    candidates = [
        item for item in items
        if item["category"] == "policy" and not item.get("summary")
    ]
    for item in candidates[:max_items]:
        try:
            resp = _get(item["url"], timeout=20)
            resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "html.parser")
            node = None
            for selector in ARTICLE_SELECTORS:
                node = soup.select_one(selector)
                if node:
                    break
            if node is None:
                continue
            text = _clean(node.get_text(" ", strip=True))
            text = text.replace(item["title"], "", 1).strip()
            if len(text) >= 20:
                item["summary"] = text[:140]
        except Exception:
            continue


def collect_all(
    config: dict[str, Any],
    themes: list[Theme],
    target_date: date,
    news_end: date | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    start = target_date - timedelta(days=int(config.get("news_lookback_days", 7)))
    # 新闻/快讯/国际要闻是实时流：最新快照可取到今天（含周末），历史归档仍按各自日期冻结。
    end = news_end if news_end is not None else target_date
    failures: list[str] = []
    raw: list[dict[str, Any]] = []

    policy_sources = set(config["sources"].get("policy", []))
    policy_map = {
        "gov_cn": ("中国政府网", fetch_gov_policy),
        "pboc": ("中国人民银行", fetch_pboc),
        "csrc": ("中国证监会", fetch_csrc),
        "stats": ("国家统计局", fetch_stats),
        "people_cn": ("人民日报", fetch_people_cn),
        "xinhua": ("新华社", fetch_xinhua),
    }
    for key, (label, fn) in policy_map.items():
        if key not in policy_sources:
            continue
        try:
            raw.extend(fn(start, end))
        except Exception as exc:
            failures.append(f"政策源 {label}: {type(exc).__name__}")

    if "eastmoney_report" in set(config["sources"].get("research", [])):
        try:
            raw.extend(fetch_research(start, end))
        except Exception as exc:
            failures.append(f"研报源 东方财富: {type(exc).__name__}")

    if "wallstreetcn" in set(config["sources"].get("news", [])):
        try:
            wscn_items = fetch_wallstreetcn(start, end)
            if not wscn_items:
                failures.append(f"快讯源 华尔街见闻: 实时流未覆盖 {end}，该源今日缺失")
            raw.extend(wscn_items)
        except Exception as exc:
            failures.append(f"快讯源 华尔街见闻: {type(exc).__name__}")

    if "cls" in set(config["sources"].get("news", [])):
        try:
            cls_items = fetch_cls(start, end)
            if not cls_items:
                failures.append(f"快讯源 财联社: 未覆盖 {end}，该源今日缺失")
            raw.extend(cls_items)
        except Exception as exc:
            failures.append(f"快讯源 财联社: {type(exc).__name__}")

    # 隔夜国际要闻：复用华尔街见闻全球快讯，按国际催化词过滤
    international_enabled = bool(config.get("news", {}).get("international_enabled", True))
    if (
        international_enabled
        and "wallstreetcn_intl" in set(config["sources"].get("international", []))
    ):
        intl_words = [str(w) for w in (config.get("catalyst_words_international") or [])]
        intl_items = [
            {**item, "category": "international"}
            for item in _fetch_wscn_raw(start, end)
            if _match_words(f"{item['title']} {item.get('summary', '')}", intl_words)
        ]
        if not intl_items:
            failures.append("国际要闻: 今日无命中国际催化词的条目")
        raw.extend(intl_items)

    source_weights = (config.get("news") or {}).get("source_weights") or {}
    research_min_dims = int((config.get("news") or {}).get("research_match_min_dims", 2))

    # 按 (category, url) 去重，允许同一 URL 分属快讯与国际两个分类
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in raw:
        if not item.get("url") or not item.get("title"):
            continue
        deduped.setdefault((item["category"], item["url"]), item)

    items: list[dict[str, Any]] = []
    for item in deduped.values():
        try:
            pub_date = date.fromisoformat(item["published_at"][:10])
        except ValueError:
            continue
        if pub_date < start:
            continue
        category = item["category"]
        text = f"{item['title']} {item.get('summary', '')}"
        if category == "research":
            hits = match_research(
                item["title"],
                item.get("summary", ""),
                item.get("industry", ""),
                themes,
                min_dims=research_min_dims,
            )
        else:
            hits = match_themes(text, themes)
        # 研报/快讯只保留命中主线催化词的条目；政策权威源无条件保留；国际要闻已按国际词过滤，保留全部
        if category in ("research", "news") and not hits:
            continue
        if category == "research" and item.get("rating"):
            if any(word in item["rating"] for word in ("买入", "增持", "推荐", "强推", "优于大市")):
                sentiment = "likely_positive"
            elif any(word in item["rating"] for word in ("减持", "卖出", "回避")):
                sentiment = "negative"
            else:
                sentiment = "neutral"
        else:
            sentiment = classify_sentiment(text)
        item["matched_themes"] = ",".join(sorted(hits, key=lambda k: -hits[k]))
        item["sentiment"] = sentiment
        item["source_weight"] = int(source_weights.get(item["source"], 1))
        items.append(item)

    assign_event_keys(items)
    return items, failures

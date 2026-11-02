"""配置与主线池加载。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

# 宽基指数不得作为主线载体
BROAD_INDEX_CODES = {
    "000001", "000016", "000300", "000510", "000852", "000905",
    "399001", "399005", "399006", "399300",
}

VALID_TIERS = {"core", "watch", "retired"}
VALID_CARRIER_TYPES = {"index", "basket"}


@dataclass
class Theme:
    id: str
    name: str
    narrative: str
    tier: str
    carrier_type: str
    index_code: str | None
    leaders: list[dict[str, str]]
    catalyst_words: list[str]
    limit_up_tags: list[str]
    primary_leader_map: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    @property
    def carrier_label(self) -> str:
        if self.carrier_type == "index":
            return f"指数 {self.index_code}"
        return "龙头等权组合"

    @property
    def leader_codes(self) -> list[str]:
        return [item["code"] for item in self.leaders]

    def leader_name(self, code: str) -> str:
        for item in self.leaders:
            if item["code"] == code:
                return item["name"]
        return code


def load_yaml(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_config(root: str | Path = ROOT) -> dict[str, Any]:
    root = Path(root)
    cfg = load_yaml(root / "config.yaml")
    cfg["_root"] = str(root)
    cfg["themes_file"] = str(root / cfg["themes_file"])
    cfg["holdings_file"] = str(root / cfg["holdings_file"])
    cfg["output"]["sqlite_path"] = str(root / cfg["output"]["sqlite_path"])
    cfg["logging"]["dir"] = str(root / cfg["logging"]["dir"])
    return cfg


def load_themes(config: dict[str, Any]) -> list[Theme]:
    raw = load_yaml(config["themes_file"]).get("themes") or []
    themes: list[Theme] = []
    seen_ids: set[str] = set()
    seen_leaders: dict[str, str] = {}
    for item in raw:
        theme_id = str(item["id"])
        if theme_id in seen_ids:
            raise ValueError(f"主线 id 重复: {theme_id}")
        seen_ids.add(theme_id)

        tier = str(item.get("tier", "core"))
        if tier not in VALID_TIERS:
            raise ValueError(f"主线 {theme_id} 的 tier 非法: {tier}")

        carrier = item.get("carrier") or {}
        carrier_type = str(carrier.get("type", ""))
        if carrier_type not in VALID_CARRIER_TYPES:
            raise ValueError(f"主线 {theme_id} 的载体类型非法: {carrier_type}")

        index_code = carrier.get("index_code")
        if carrier_type == "index":
            if not index_code:
                raise ValueError(f"主线 {theme_id} 使用 index 载体但缺少 index_code")
            index_code = str(index_code)
            if index_code in BROAD_INDEX_CODES:
                raise ValueError(f"主线 {theme_id} 不得使用宽基指数作为载体: {index_code}")

        leaders = item.get("leaders") or []
        for leader in leaders:
            code = str(leader["code"])
            if code in seen_leaders:
                raise ValueError(
                    f"龙头 {code} 重复归属主线 {seen_leaders[code]} 与 {theme_id}，"
                    "请通过 primary_leader_map 明确主归属"
                )
            seen_leaders[code] = theme_id

        themes.append(
            Theme(
                id=theme_id,
                name=str(item["name"]),
                narrative=str(item.get("narrative", "")),
                tier=tier,
                carrier_type=carrier_type,
                index_code=index_code,
                leaders=[{"code": str(x["code"]), "name": str(x["name"])} for x in leaders],
                catalyst_words=[str(x) for x in (item.get("catalyst_words") or [])],
                limit_up_tags=[str(x) for x in (item.get("limit_up_tags") or [])],
                primary_leader_map={str(k): str(v) for k, v in (item.get("primary_leader_map") or {}).items()},
                notes=str(item.get("notes", "")),
            )
        )
    if not themes:
        raise ValueError("themes.yaml 未配置任何主线")
    return themes


def load_holdings(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = load_yaml(config["holdings_file"]).get("holdings") or []
    out: list[dict[str, Any]] = []
    for item in raw:
        theme = item.get("theme")
        out.append(
            {
                "code": str(item.get("code", "")),
                "name": str(item.get("name", "")),
                "theme": str(theme) if theme else None,
                "risk_note": str(item.get("risk_note", "")),
            }
        )
    return out

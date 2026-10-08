"""搜索结果解析器（T04 任务 3/4）：mtop 搜索响应 → ItemRecord 列表。

纯函数、幂等：同一段原始数据解析结果恒定一致（T04 验收标准 2）。
字段结构基于 T01 实测（docs/字段核对清单.md）；页面改版时只需修此模块。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

WANT_TAG = re.compile(r"(\d+)\s*人想要")
SELLER_ID_PIC = re.compile(r"/i\d/(\d+)/")

# 核心字段（完整率统计口径，T04 验收标准 1）；seller_nick/seller_id 已知列表页有缺口，单列
CORE_FIELDS = ("item_id", "title", "price", "want_count", "location", "pic_url", "item_url")


@dataclass(slots=True)
class ItemRecord:
    item_id: str
    title: str | None
    price: float | None
    want_count: int | None
    view_count: int | None        # 列表页不提供 → FR-4 详情采集补齐
    publish_time: str | None      # 同上
    status: str                   # 在售 / 已售出
    seller_nick: str | None
    seller_id: str | None
    location: str | None
    pic_url: str | None
    item_url: str
    tags: list[str] = field(default_factory=list)   # 页面标签（成色/品牌/信用等，供筛选与分析）
    source_keyword: str = ""
    source_sort: str = ""
    page_number: int = 0
    crawled_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ParseOutcome:
    items: list[ItemRecord]
    missing_field_count: dict[str, int]   # 各字段为空的条数（容错计数，不中断）
    raw_item_count: int                   # 去重前定位到的条目数
    duplicate_count: int = 0


def _tag_contents(it: dict) -> list[str]:
    contents: list[str] = []
    fish_tags = it.get("fishTags") or {}
    if not isinstance(fish_tags, dict):
        return contents
    for v in fish_tags.values():
        if not isinstance(v, dict):
            continue
        for t in (v.get("tagList") or []):
            if isinstance(t, dict):
                c = (t.get("data") or {}).get("content")
                if c:
                    contents.append(str(c))
    return contents


def _parse_price(it: dict) -> float | None:
    raw = (it.get("detailParams") or {}).get("soldPrice") or it.get("soldPrice")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def is_item_dict(d: Any) -> bool:
    """形如商品条目判定。

    已知行为：真实响应中每个商品会被命中两次——外层条目与其内嵌 detailParams
    片段（同样含 itemId+title）。二者嵌套在同一子树内，walk 先外后内，
    去重恒保留信息更丰富的外层条目（parse_search_payload 内按 itemId 去重）。
    """
    return (isinstance(d, dict)
            and isinstance(d.get("itemId"), (str, int))
            and bool(d.get("title")))


def walk_items(node: Any, out: list[dict]) -> None:
    """深度优先收集一切形如商品条目的 dict（嵌套位置无关）。"""
    if isinstance(node, dict):
        if is_item_dict(node):
            out.append(node)
        for v in node.values():
            walk_items(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_items(v, out)


def parse_item(it: dict, *, keyword: str, sort: str, page_number: int,
               crawled_at: str) -> ItemRecord:
    tags = _tag_contents(it)
    want_match = next((m for c in tags if (m := WANT_TAG.search(c))), None)
    want = int(want_match.group(1)) if want_match else (0 if tags else None)
    uid = SELLER_ID_PIC.search(it.get("picUrl") or "")
    item_id = str(it["itemId"])
    nick = it.get("userNickName") or (it.get("detailParams") or {}).get("userNick")
    return ItemRecord(
        item_id=item_id,
        title=str(it["title"]).strip() or None,
        price=_parse_price(it),
        want_count=want,
        view_count=None,
        publish_time=None,
        status="已售出" if any("已售" in c for c in tags) else "在售",
        seller_nick=str(nick) if nick else None,
        seller_id=uid.group(1) if uid else None,
        location=it.get("area") or None,
        pic_url=it.get("picUrl") or None,
        item_url=f"https://www.goofish.com/item?id={item_id}",
        tags=tags,
        source_keyword=keyword,
        source_sort=sort,
        page_number=page_number,
        crawled_at=crawled_at,
    )


def parse_search_payload(payload: Any, *, keyword: str, sort: str = "",
                         page_number: int = 0, crawled_at: str = "") -> ParseOutcome:
    """解析一份已 json.loads 的搜索接口响应。"""
    raws: list[dict] = []
    walk_items(payload, raws)

    items: list[ItemRecord] = []
    seen: set[str] = set()
    missing: dict[str, int] = {}
    duplicates = 0
    for it in raws:
        rec = parse_item(it, keyword=keyword, sort=sort,
                         page_number=page_number, crawled_at=crawled_at)
        if rec.item_id in seen:
            duplicates += 1
            continue
        seen.add(rec.item_id)
        for f in CORE_FIELDS + ("seller_nick", "seller_id"):
            if getattr(rec, f) is None:
                missing[f] = missing.get(f, 0) + 1
        items.append(rec)
    return ParseOutcome(items=items, missing_field_count=missing,
                        raw_item_count=len(raws), duplicate_count=duplicates)


def apply_filters(items: list[ItemRecord], task) -> list[ItemRecord]:
    """任务筛选条件的落地实现（T04 决策：解析后过滤，等效且比 UI 点击稳定）。"""
    out = items
    if task.price_min is not None:
        out = [r for r in out if r.price is not None and r.price >= task.price_min]
    if task.price_max is not None:
        out = [r for r in out if r.price is not None and r.price <= task.price_max]
    if task.stuff:
        out = [r for r in out if any(task.stuff in t for t in r.tags)]
    if task.location:
        out = [r for r in out if r.location and task.location in r.location]
    return out

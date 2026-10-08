"""PoC-3：字段核对分析（按 PoC-2/4 实测的真实数据结构）。

数据源：
- data/samples/raw/search_api_*.json —— 搜索列表接口响应（mtop.taobao.idlemtopsearch.pc.search）
- data/samples/poc2_result_*.json    —— DOM 抽取结果与耗时

输出：data/samples/field_check_result.json（字段核对结论，供《字段核对清单》引用）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from poc_common import RAW, SAMPLES, log, save_result

FIELD_LABELS = {
    "item_id": "商品唯一标识",
    "title": "商品标题",
    "price": "当前售价",
    "want_count": "想要数",
    "view_count": "浏览量",
    "publish_time": "发布时间",
    "status": "商品状态(在售/已售)",
    "seller_nick": "卖家昵称",
    "seller_id": "卖家标识",
    "location": "所在地",
    "pic_url": "商品图片URL",
    "item_url": "商品链接",
}


def collect_search_items() -> list[dict]:
    """从列表接口样本中定位商品条目（itemId + title 双键判定）。"""
    items: list[dict] = []
    for f in sorted(RAW.glob("search_api_*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        found: list[dict] = []

        def walk(n):
            if isinstance(n, dict):
                if "itemId" in n and "title" in n:
                    found.append(n)
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)

        walk(data)
        if found:
            log(f"  {f.name}: {len(found)} 个商品条目")
            items.extend(found)
    seen, uniq = set(), []
    for it in items:
        if it.get("itemId") and it["itemId"] not in seen:
            seen.add(it["itemId"])
            uniq.append(it)
    return uniq


def parse_item(it: dict) -> dict:
    """按实测结构抽取 12 个核对字段。"""
    dp = it.get("detailParams") or {}
    tags: list[str] = []
    for v in (it.get("fishTags") or {}).values():
        for t in (v.get("tagList") or []):
            c = (t.get("data") or {}).get("content")
            if c:
                tags.append(c)

    m_want = next((re.search(r"(\d+)\s*人想要", c) for c in tags
                   if re.search(r"\d+\s*人想要", c)), None)
    uid_m = re.search(r"/i\d/(\d+)/", it.get("picUrl") or "")

    return {
        "item_id": it.get("itemId"),
        "title": (it.get("title") or "").strip() or None,
        "price": float(dp["soldPrice"]) if dp.get("soldPrice") else None,
        "want_count": int(m_want.group(1)) if m_want else (0 if tags else None),
        "view_count": None,                       # 列表接口不提供 → 详情页 browseCnt
        "publish_time": None,                     # 列表接口不提供 → 详情页 GMT_CREATE_DATE_KEY
        "status": "已售出" if any("已售" in c for c in tags) else "在售",
        "seller_nick": it.get("userNickName") or dp.get("userNick"),
        "seller_id": uid_m.group(1) if uid_m else None,
        "location": it.get("area"),
        "pic_url": it.get("picUrl"),
        "item_url": f"https://www.goofish.com/item?id={it.get('itemId')}",
    }


# 字段 → 可得层级（基于 PoC-2/4 实测结论）
FIELD_SOURCE = {
    "item_id":     "列表页·接口(itemId)",
    "title":       "列表页·接口(title)",
    "price":       "列表页·接口(detailParams.soldPrice)",
    "want_count":  "列表页·接口(fishTags 标签 N人想要，无标签计 0)",
    "view_count":  "详情页·接口(browseCnt) —— FR-4 详情采集补齐",
    "publish_time": "详情页·接口(GMT_CREATE_DATE_KEY/proPolishTime) —— FR-4 详情采集补齐",
    "status":      "详情页·接口(itemStatus/itemStatusStr)；列表页仅能以“已售”标签兜底",
    "seller_nick": "列表页·接口(userNickName)",
    "seller_id":   "列表页·接口(picUrl 路径推断，26/30 命中)",
    "location":    "列表页·接口(area)",
    "pic_url":     "列表页·接口(picUrl)",
    "item_url":    "由 itemId 构造",
}


def main() -> None:
    raw_items = collect_search_items()
    log(f"去重后商品条目: {len(raw_items)}")
    parsed = [parse_item(it) for it in raw_items]

    n = len(parsed)
    coverage = {}
    for field in FIELD_LABELS:
        avail = sum(1 for r in parsed if r.get(field) is not None)
        coverage[field] = {
            "label": FIELD_LABELS[field],
            "source": FIELD_SOURCE[field],
            "items_available": avail,
            "items_total": n,
            "coverage_pct": round(avail / n * 100, 1) if n else 0.0,
            "sample": str(parsed[0].get(field))[:80] if parsed and parsed[0].get(field) else None,
        }

    # DOM 抽取结果（PoC-2）：item_url / item_id / 标题 在 DOM 层亦可直接获得
    dom_files = sorted(SAMPLES.glob("poc2_result_*.json"))
    dom_info = {}
    timings = {}
    if dom_files:
        dom = json.loads(dom_files[-1].read_text(encoding="utf-8"))
        dom_info = {"dom_card_count": dom.get("dom_card_count"),
                    "dom_selector": 'a[href*="item?id="]'}
        timings = {k: v for k, v in dom.items() if k.endswith("seconds")}

    fields_on_list = sum(1 for f in FIELD_LABELS
                         if "列表页" in FIELD_SOURCE[f] or "构造" in FIELD_SOURCE[f])
    result = {
        "items_total": n,
        "fields_on_list_page": fields_on_list,
        "fields_total": len(FIELD_LABELS),
        "list_page_coverage_pct": round(fields_on_list / len(FIELD_LABELS) * 100, 1),
        "timings": timings,
        "dom": dom_info,
        "field_coverage": coverage,
    }
    save_result("field_check_result.json", result)

    log(f"列表页直接可得字段: {fields_on_list}/12 "
        f"（view_count/publish_time/status 由详情页补齐）")
    for f, info in coverage.items():
        log(f"  {f:<14} {info['coverage_pct']:>5}%  [{info['source'][:40]}]")


if __name__ == "__main__":
    main()

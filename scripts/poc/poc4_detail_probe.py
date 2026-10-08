"""PoC-4：详情页字段验证 + 搜索页滚动加载测试。

目的：
1. 验证浏览量/发布时间/卖家id/商品状态是否在商品详情页可得（列表页缺失字段的补充层级）；
2. 验证搜索结果页是否为无限滚动加载（T04 翻页实现方式的依据）；
3. 测量详情页加载耗时（T11 限速参数依据）。

会话复用已由 PoC-2 验证，本脚本全程 headless。
"""
from __future__ import annotations

import json
import re
import time

from playwright.sync_api import sync_playwright

from poc_common import (
    HOME_URL, SAMPLES, SCREENS, SEARCH_URL, USER_DATA,
    log, save_result, save_sample, ts,
)

KEY_PATTERNS = re.compile(
    r"view|browse|pub(time)?|publish|time|date|nick|userid|status|sold|want", re.I)


def item_ids_from_poc2() -> list[str]:
    files = sorted(SAMPLES.glob("poc2_result_*.json"))
    if not files:
        return []
    dom = json.loads(files[-1].read_text(encoding="utf-8"))
    ids = []
    for card in dom.get("dom_cards", []):
        m = re.search(r"[?&]id=(\d+)", card.get("href", ""))
        if m:
            ids.append(m.group(1))
    return ids[:3]


def scan_keys(node, found: dict, path: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            if KEY_PATTERNS.search(k) and not isinstance(v, (dict, list)):
                found.setdefault(k, str(v)[:60])
            scan_keys(v, found, f"{path}.{k}")
    elif isinstance(node, list):
        for v in node:
            scan_keys(v, found, f"{path}[]")


def probe_detail(page, item_id: str, stamp: str, idx: int) -> dict:
    captured: list[str] = []

    def on_response(resp):
        if len(captured) >= 10:
            return
        url = resp.url
        try:
            ctype = (resp.headers or {}).get("content-type", "")
        except Exception:
            ctype = ""
        if "json" in ctype and "mtop" in url:
            try:
                body = resp.text()
                if len(body) >= 500:
                    name = f"detail_api_{idx:02d}_{len(captured)}.json"
                    save_sample(name, body)
                    captured.append(name)
            except Exception:
                pass

    page.on("response", on_response)
    t0 = time.monotonic()
    page.goto(f"https://www.goofish.com/item?id={item_id}",
              wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(6000)
    load = round(time.monotonic() - t0, 2)
    page.screenshot(path=str(SCREENS / f"detail_{idx}_{stamp}.png"))

    found: dict = {}
    for name in captured:
        try:
            data = json.loads((RAW_DIR := SAMPLES / "raw" / name).read_text(encoding="utf-8"))
        except Exception:
            continue
        scan_keys(data, found)

    body_text = ""
    try:
        body_text = page.evaluate("() => document.body.innerText.slice(0, 4000)")
    except Exception:
        pass
    dom_hits = {pat: bool(re.search(pat, body_text))
                for pat in (r"浏览[:：]?\s*\d+", r"\d+\s*人想要", r"浏览量", r"\d{4}-\d{2}-\d{2}")}

    page.remove_listener("response", on_response)
    return {"item_id": item_id, "load_seconds": load,
            "captured": captured, "api_key_hits": found, "dom_text_hits": dom_hits}


def scroll_test(page) -> dict:
    t0 = time.monotonic()
    page.goto(SEARCH_URL.format(kw="iPhone"), wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(6000)
    counts = []
    js = "() => document.querySelectorAll('a[href*=\"item?id=\"]').length"
    counts.append(page.evaluate(js))
    for i in range(3):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(4500)
        counts.append(page.evaluate(js))
    return {"anchor_counts_by_scroll": counts,
            "infinite_scroll_works": counts[-1] > counts[0],
            "total_seconds": round(time.monotonic() - t0, 2)}


def main() -> None:
    stamp = ts()
    ids = item_ids_from_poc2()
    log(f"待验证商品: {ids}")
    result: dict = {"script": "poc4_detail_probe", "timestamp": stamp, "items": []}

    # headless 会被闲鱼风控识别为“非法访问”（PoC 实测），必须 headed 模式
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA), headless=False, locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)

        for idx, item_id in enumerate(ids):
            log(f"探测详情页 [{idx}] item_id={item_id} ...")
            result["items"].append(probe_detail(page, item_id, stamp, idx))
            page.wait_for_timeout(4000)  # 详情间限速

        log("搜索页滚动加载测试 ...")
        result["scroll_test"] = scroll_test(page)
        ctx.close()

    save_result(f"poc4_detail_result_{stamp}.json", result)
    for it in result["items"]:
        log(f"item {it['item_id']}: 详情页 {it['load_seconds']}s, "
            f"API键命中 {sorted(it['api_key_hits'].keys())[:12]}")
    log(f"滚动测试: {result['scroll_test']}")
    log("PoC-4 完成")


if __name__ == "__main__":
    main()

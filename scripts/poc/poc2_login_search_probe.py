"""PoC-2：扫码登录 + 搜索列表页字段核对（T01 任务 2/3/4/5/6）。

流程：
1. 弹出有头浏览器访问 goofish.com，检测登录态；未登录则点击登录入口，等待人工扫码；
2. 登录成功后重启上下文（新进程语义），验证持久化 user_data_dir 可复用会话（T01 验收标准 1）；
3. 访问搜索结果页，被动记录页面自身发出的 JSON 响应（不做任何接口逆向），
   归档原始 HTML / JSON / 截图样本；
4. DOM 与内嵌 JSON 双路径抽取字段，测量各阶段耗时。

退出码：0 成功；10 登录超时；20 搜索页不可达。
"""
from __future__ import annotations

import argparse
import json
import time

from playwright.sync_api import sync_playwright

from poc_common import (
    HOME_URL, SAMPLES, SCREENS, SEARCH_URL, USER_DATA,
    log, save_result, save_sample, ts,
)

# 淘宝系登录后会出现的用户 Cookie（登录态启发式信号）
LOGIN_COOKIES = {"unb", "dnk", "tracknick", "lgc"}


def has_user_cookie(ctx) -> bool:
    names = {c["name"] for c in ctx.cookies()}
    return bool(names & LOGIN_COOKIES)


def login_text_visible(page) -> bool:
    for text in ("亲，请登录", "请登录"):
        try:
            if page.get_by_text(text, exact=False).count() > 0:
                return True
        except Exception:
            continue
    return False


def wait_for_login(ctx, max_wait: int) -> bool:
    log("=" * 60)
    log("请在弹出的浏览器窗口中完成【扫码登录】（闲鱼/淘宝 App 扫码）")
    log(f"脚本每 3 秒检测一次登录态，最长等待 {max_wait} 秒 ...")
    log("=" * 60)
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_wait:
        if has_user_cookie(ctx):
            log("检测到用户 Cookie，登录成功！")
            return True
        time.sleep(3)
    return False


def extract_dom_cards(page) -> list[dict]:
    js = """
    () => {
      const anchors = [...document.querySelectorAll('a[href*="item?id="], a[href*="item.htm?id="]')];
      return anchors.slice(0, 80).map(a => ({
        href: a.href,
        text: (a.innerText || '').replace(/\\s+/g, ' | ').slice(0, 300),
        img: a.querySelector('img') ? a.querySelector('img').src : null,
      }));
    }
    """
    try:
        return page.evaluate(js)
    except Exception as exc:
        log(f"DOM 抽取失败: {exc}")
        return []


def selector_diagnostics(page) -> dict:
    out = {}
    for sel in (
        'a[href*="item?id="]', 'a[href*="item.htm"]', '[class*="item"]',
        '[class*="card"]', '[class*="Card"]', '[class*="cell"]', '[class*="Item"]',
    ):
        try:
            out[sel] = page.locator(sel).count()
        except Exception:
            out[sel] = -1
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", default="iPhone")
    parser.add_argument("--login-wait", type=int, default=300, help="等待扫码秒数")
    parser.add_argument("--skip-restart-check", action="store_true",
                        help="跳过『重启复用会话』验证")
    args = parser.parse_args()

    stamp = ts()
    result: dict = {"script": "poc2_login_search_probe", "timestamp": stamp,
                    "keyword": args.keyword}

    with sync_playwright() as p:
        # ---- 阶段 1：首次启动（有头），扫码登录 ----
        log("启动浏览器（首次会话，headed 模式）...")
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA), headless=False, locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        t0 = time.monotonic()
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5000)
        result["home_load_seconds"] = round(time.monotonic() - t0, 2)

        if has_user_cookie(ctx) and not login_text_visible(page):
            log("已有有效登录态，跳过扫码。")
        else:
            # 尝试点击登录入口，否则由用户在窗口内自行点击
            clicked = False
            for text in ("亲，请登录", "请登录", "登录"):
                try:
                    loc = page.get_by_text(text, exact=True).first
                    if loc.count() >= 0 and loc.is_visible():
                        loc.click(timeout=5000)
                        clicked = True
                        log(f"已点击登录入口: {text!r}")
                        break
                except Exception:
                    continue
            if not clicked:
                log("未自动定位到登录入口，请在窗口中手动点击『登录』。")
            page.screenshot(path=str(SCREENS / f"login_prompt_{stamp}.png"))
            if not wait_for_login(ctx, args.login_wait):
                ctx.close()
                save_result(f"poc2_result_{stamp}.json", {**result, "status": "LOGIN_TIMEOUT"})
                log("FAIL: 登录超时（退出码 10）")
                raise SystemExit(10)

        # ---- 阶段 2：重启复用会话验证（T01 验收标准 1）----
        ctx.close()
        if args.skip_restart_check:
            log("跳过重启复用验证。")
        else:
            log("关闭浏览器，模拟新进程重启，验证 user_data_dir 会话复用 ...")
            time.sleep(2)
            ctx = p.chromium.launch_persistent_context(
                str(USER_DATA), headless=True, locale="zh-CN",
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            t0 = time.monotonic()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(5000)
            result["restart_session_reused"] = bool(
                has_user_cookie(ctx) and not login_text_visible(page))
            result["restart_load_seconds"] = round(time.monotonic() - t0, 2)
            log(f"重启后会话复用: {result['restart_session_reused']}")
            ctx.close()
            if not result["restart_session_reused"]:
                save_result(f"poc2_result_{stamp}.json",
                            {**result, "status": "SESSION_NOT_REUSED"})
                log("FAIL: 重启后会话失效（退出码 11）")
                raise SystemExit(11)

        # ---- 阶段 3：搜索页采集（有头 + 响应被动记录）----
        log("重启浏览器进入搜索采集阶段 ...")
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA), headless=False, locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        captured: list[dict] = []

        def on_response(resp):
            url = resp.url
            ctype = ""
            try:
                ctype = (resp.headers or {}).get("content-type", "")
            except Exception:
                pass
            if len(captured) >= 30:
                return
            if "json" not in ctype:
                return
            if not any(k in url for k in ("mtop", "h5api", "search", "recommend")):
                return
            try:
                body = resp.text()
                if len(body) < 500:
                    return
                idx = len(captured)
                name = f"search_api_{idx:02d}.json"
                save_sample(name, body)
                captured.append({"index": idx, "url": url[:300],
                                 "size": len(body), "file": name})
            except Exception:
                pass

        page.on("response", on_response)

        t0 = time.monotonic()
        page.goto(SEARCH_URL.format(kw=args.keyword), wait_until="domcontentloaded",
                  timeout=60_000)
        page.wait_for_timeout(8000)  # SPA 水合
        result["search_load_seconds"] = round(time.monotonic() - t0, 2)
        result["search_final_url"] = page.url

        if "login" in page.url:
            result["status"] = "SEARCH_REDIRECT_LOGIN"
            save_result(f"poc2_result_{stamp}.json", result)
            log("FAIL: 搜索页被重定向到登录页（退出码 20）")
            ctx.close()
            raise SystemExit(20)

        result["dom_selector_counts"] = selector_diagnostics(page)
        result["dom_cards"] = extract_dom_cards(page)
        result["dom_card_count"] = len(result["dom_cards"])
        log(f"DOM 抽取到商品卡片: {result['dom_card_count']} 个")

        save_sample(f"search_page_{stamp}.html", page.content())
        page.screenshot(path=str(SCREENS / f"search_results_{stamp}.png"), full_page=False)

        # 翻页探测（第二页样本 + 加载耗时，为 T11 限速参数提供依据）
        try:
            nxt = page.get_by_text("下一页", exact=False).first
            if nxt.count() >= 0 and nxt.is_visible():
                t0 = time.monotonic()
                nxt.click(timeout=5000)
                page.wait_for_timeout(6000)
                result["page2_load_seconds"] = round(time.monotonic() - t0, 2)
                result["page2_dom_card_count"] = len(extract_dom_cards(page))
                save_sample(f"search_page2_{stamp}.html", page.content())
                page.screenshot(path=str(SCREENS / f"search_page2_{stamp}.png"))
                log(f"第二页采集: {result['page2_dom_card_count']} 个卡片, "
                    f"耗时 {result['page2_load_seconds']}s")
        except Exception as exc:
            log(f"翻页探测跳过: {exc}")

        result["captured_api_responses"] = captured
        ctx.close()

    result["status"] = "OK"
    result["dom_cards_sample"] = result.get("dom_cards", [])[:5]
    save_result(f"poc2_result_{stamp}.json", result)
    log("PoC-2 完成")


if __name__ == "__main__":
    main()

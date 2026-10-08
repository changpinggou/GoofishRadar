"""PoC-1：免登录首页探测。

目的（对应需求单 T01 任务 4/5/6）：
1. 确认 goofish.com 无登录态下可见的内容与登录入口元素；
2. 探测页面内嵌数据全局变量（window.__XXX__），确定数据获取路径；
3. 测量首页加载耗时，为限速参数提供基线；
4. 归档原始 HTML + 截图样本。
"""
from __future__ import annotations

from playwright.sync_api import sync_playwright

from poc_common import HOME_URL, SAMPLES, USER_DATA, log, save_result, save_sample, ts

# 探测是否存在页面内嵌数据（优先解析内嵌 JSON，DOM 兜底 —— T01 任务 5）
GLOBALS_JS = """
() => {
  const found = [];
  for (const k of Object.keys(window)) {
    if (/^(__)\\w+(__)$/.test(k) || /INITIAL_STATE|NUXT|NEXT_DATA|APP_DATA|__data/i.test(k)) {
      try {
        const s = JSON.stringify(window[k]);
        found.push({ key: k, type: typeof window[k], approx_size: s ? s.length : 0 });
      } catch (e) {
        found.push({ key: k, type: typeof window[k], approx_size: -1 });
      }
    }
  }
  return found;
}
"""

# 探测登录入口元素（登录态检测的启发式依据）
LOGIN_HINT_JS = """
() => {
  const hits = [];
  document.querySelectorAll('a, button, span').forEach(el => {
    const t = (el.textContent || '').trim();
    if (t && t.length <= 16 && /登录|登陆|请登录/.test(t)) {
      hits.push({ tag: el.tagName, text: t, cls: String(el.className || '').slice(0, 80) });
    }
  });
  return hits.slice(0, 10);
}
"""


def main() -> None:
    stamp = ts()
    result: dict = {"script": "poc1_homepage_probe", "timestamp": stamp}

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA), headless=True, locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        log(f"访问 {HOME_URL} ...")
        import time
        t0 = time.monotonic()
        resp = page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        t_load = time.monotonic() - t0
        page.wait_for_timeout(6000)  # SPA 水合等待

        result["http_status"] = resp.status if resp else None
        result["final_url"] = page.url
        result["page_title"] = page.title()
        result["load_seconds"] = round(t_load, 2)
        result["embedded_globals"] = page.evaluate(GLOBALS_JS)
        result["login_hints"] = page.evaluate(LOGIN_HINT_JS)
        result["cookies"] = [c["name"] for c in ctx.cookies("https://www.goofish.com")]

        html = page.content()
        save_sample(f"homepage_{stamp}.html", html)
        page.screenshot(path=str(SAMPLES / "screens" / f"homepage_{stamp}.png"), full_page=False)

        ctx.close()

    result["html_size"] = len(html)
    save_result(f"poc1_homepage_result_{stamp}.json", result)
    log(f"首页加载耗时 {t_load:.1f}s，标题: {result['page_title']!r}")
    log("PoC-1 完成")


if __name__ == "__main__":
    main()

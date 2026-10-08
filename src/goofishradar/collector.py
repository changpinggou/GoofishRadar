"""搜索列表页采集器（T04 / FR-3）：按关键词任务逐个搜索、翻页、限速、落盘、解析。

T01/T02 实测约束（必须遵守）：
- 必须 headed 模式（headless 会被风控拦截）；
- 翻页为页码控件（无「下一页」文本）：右箭头按钮 + SPA detach 防御（JS 点击）；
- 商品数据来自页面自身的 mtop JSON 响应（被动监听，无任何接口逆向）；
- 遇风控/验证码立即抛 RiskBlockedError 中止全部采集（T04 任务 6；退避策略归 T11）。
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from .config import KeywordTask, Settings, enabled_tasks
from .logsetup import get_logger
from .parser import ItemRecord, apply_filters, parse_search_payload
from .paths import DATA_DIR, USER_DATA_DIR
from .session import VIEWPORT, detect_risk_block

SEARCH_URL = "https://www.goofish.com/search?q={kw}"

# 排序实现（T04 实测：排序为四个下拉选择器「综合/新发布/价格/新降价」，无「最多想要」项）：
# sort 值 → (触发器文案, 选项文案)；触发器为 None = 用页面默认综合序。
# most_want 保留为兼容值映射到默认序——畅销度评分算自想要数，不依赖采集排序。
SORT_SELECTORS = {
    "comprehensive": (None, None),
    "most_want": (None, None),
    "recent_active": ("综合", "最近活跃"),
    "newest": ("新发布", "最新"),
    "price_asc": ("价格", "价格从低到高"),
    "price_desc": ("价格", "价格从高到低"),
}


class RiskBlockedError(RuntimeError):
    """触发风控/验证码 —— 采集必须立即中止（T04 任务 6）。"""


def sanitize_keyword(keyword: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", keyword.strip())[:40] or "unnamed"


class RateLimiter:
    """页间随机延时（配置区间内均匀分布）。"""

    def __init__(self, lo: float, hi: float):
        self.lo, self.hi = float(lo), float(hi)

    def next_delay(self) -> float:
        return random.uniform(self.lo, self.hi)

    def wait(self) -> float:
        d = self.next_delay()
        time.sleep(d)
        return d


@dataclass
class KeywordRunResult:
    keyword: str
    pages_attempted: int = 0
    pages_ok: int = 0
    degraded_pages: int = 0          # 未捕获接口响应、无法结构化解析的页
    items: list[ItemRecord] = field(default_factory=list)
    missing_field_count: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def parse_success_rate(self) -> float:
        return self.pages_ok / self.pages_attempted if self.pages_attempted else 0.0


class SearchCollector:
    def __init__(self, settings: Settings, log=None):
        self.settings = settings
        self.log = log or get_logger()
        self.limits = settings.limits

    # ---- 对外入口 ----

    def collect_keywords(self, tasks: list[KeywordTask]) -> tuple[list[KeywordRunResult], bool]:
        """逐关键词顺序采集（不并发）。返回 (逐词结果, 是否因风控中止)。"""
        results: list[KeywordRunResult] = []
        aborted = False
        active = enabled_tasks(tasks)
        total = len(active)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(USER_DATA_DIR), headless=False, locale="zh-CN", viewport=VIEWPORT)
            try:
                for idx, task in enumerate(active, 1):
                    if aborted:
                        break
                    try:
                        results.append(self.collect_keyword(ctx, task, idx, total))
                    except RiskBlockedError as exc:
                        self.log.error("ALERT: 触发风控/验证码，中止本次全部采集：%s", exc)
                        results.append(KeywordRunResult(
                            keyword=task.keyword, error=f"risk_blocked: {exc}"))
                        aborted = True
                    except Exception as exc:
                        self.log.error("关键词「%s」采集失败：%s", task.keyword, exc)
                        results.append(KeywordRunResult(
                            keyword=task.keyword, error=str(exc)))
            finally:
                ctx.close()
        return results, aborted

    # ---- 单关键词采集 ----

    def collect_keyword(self, ctx, task: KeywordTask, idx: int, total: int
                        ) -> KeywordRunResult:
        day = datetime.now().strftime("%Y-%m-%d")
        raw_dir = DATA_DIR / "raw" / day / sanitize_keyword(task.keyword)
        parsed_dir = DATA_DIR / "parsed" / day
        raw_dir.mkdir(parents=True, exist_ok=True)
        parsed_dir.mkdir(parents=True, exist_ok=True)

        pages = self._page_budget(task, DATA_DIR / "raw" / day)
        res = KeywordRunResult(keyword=task.keyword)
        if pages <= 0:
            res.error = "daily_budget_exhausted"
            self.log.warning("「%s」单日页数预算已用尽，跳过", task.keyword)
            return res

        rate = RateLimiter(*self.limits.page_delay_seconds)
        crawled_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        seen_ids: set[str] = set()
        jsonl_path = parsed_dir / f"{sanitize_keyword(task.keyword)}.jsonl"

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        buffer: list[dict] = []

        def on_response(resp):
            if len(buffer) >= 200:
                return
            try:
                ctype = (resp.headers or {}).get("content-type", "")
                url = resp.url
            except Exception:
                return
            if "json" not in ctype or "mtop" not in url:
                return
            try:
                body = resp.text()
            except Exception:
                return
            if body and len(body) >= 500:
                buffer.append({"url": url, "body": body, "consumed": False})

        page.on("response", on_response)
        try:
            self.log.info("关键词 %d/%d「%s」开始（≤%d 页，排序 %s）",
                          idx, total, task.keyword, pages, task.sort)
            page.goto(SEARCH_URL.format(kw=quote(task.keyword)),
                      wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(6_000)
            self._check_risk(page, task.keyword)
            self._apply_sort(page, task.sort)

            with open(jsonl_path, "a", encoding="utf-8") as jsonl:
                for n in range(1, pages + 1):
                    if n > 1:
                        if not self._goto_page(page, n):
                            self.log.info("「%s」没有更多结果，提前结束于第 %d 页",
                                          task.keyword, n - 1)
                            break
                        page.wait_for_timeout(6_000)
                        self._check_risk(page, task.keyword)

                    res.pages_attempted += 1
                    outcome = self._parse_page(page, buffer, task, n, crawled_at,
                                               raw_dir)
                    if outcome is None:
                        res.degraded_pages += 1
                        self.log.warning("「%s」第 %d 页未捕获接口响应，记为降级页",
                                         task.keyword, n)
                    else:
                        fresh = [r for r in outcome.items if r.item_id not in seen_ids]
                        seen_ids.update(r.item_id for r in fresh)
                        for r in fresh:
                            jsonl.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
                        res.pages_ok += 1
                        res.items.extend(fresh)
                        for k, v in outcome.missing_field_count.items():
                            res.missing_field_count[k] = res.missing_field_count.get(k, 0) + v
                        self.log.info("关键词 %d/%d「%s」第 %d/%d 页：新增 %d 条（累计 %d 条）",
                                      idx, total, task.keyword, n, pages,
                                      len(fresh), len(res.items))

                    if n < pages:
                        delay = rate.wait()
                        self.log.debug("「%s」页间延时 %.1fs", task.keyword, delay)
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
            self._save_manifest(buffer, raw_dir)

        # 任务筛选（解析后过滤，等效 UI 筛选且更稳）
        res.items = apply_filters(res.items, task)
        return res

    # ---- 内部 ----

    def _page_budget(self, task: KeywordTask, day_raw_dir: Path) -> int:
        """单日预算 = max_pages_per_day - 今日已归档页数；再与任务页数取小。"""
        used = 0
        if day_raw_dir.exists():
            used = sum(1 for _ in day_raw_dir.rglob("page_*.json"))
        left = self.limits.max_pages_per_day - used
        if left <= 0:
            return 0
        return min(task.max_pages, left)

    def _check_risk(self, page, keyword: str) -> None:
        try:
            body = page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 3000) : ''")
        except Exception:
            body = ""
        if detect_risk_block(body):
            raise RiskBlockedError(f"关键词「{keyword}」页面命中风控/验证码特征")

    def _apply_sort(self, page, sort: str) -> None:
        """应用排序：对下拉选项元素直接派发完整 pointer 事件序列。

        T04 实测：排序为「综合/新发布/价格/新降价」四个下拉，但面板用常规点击
        （真实鼠标事件/hover/触发器点击）均无法展开；对 DOM 中已存在的选项元素
        直接派发 pointerdown→…→click 序列即可生效（触发器文案随之变化，可校验）。
        失败仅告警不中断（默认综合序亦可采）。
        """
        trigger, option = SORT_SELECTORS.get(sort, (None, None))
        if option is None:
            if sort == "most_want":
                self.log.info("排序 most_want：平台无「最多想要」排序项（T04 实测），"
                              "按默认综合序采集；畅销度评分算自想要数，不受影响")
            return
        try:
            step = page.evaluate(
                """(optionText) => {
                  const norm = s => (s || '').trim();
                  const opt = [...document.querySelectorAll('[class*="select-item"]')]
                    .find(e => norm(e.textContent) === optionText);
                  if (!opt) return 'no_option';
                  ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']
                    .forEach(t => opt.dispatchEvent(new PointerEvent(t, {
                      bubbles: true, cancelable: true, pointerType: 'mouse'})));
                  return 'ok';
                }""", option)
            if step != "ok":
                self.log.warning("排序「%s」选项「%s」未找到（%s），按默认顺序采集",
                                 sort, option, step)
                return
            page.wait_for_timeout(5_000)
            applied = page.evaluate(
                """(optionText) => {
                  const norm = s => (s || '').trim();
                  return [...document.querySelectorAll('[class*="select-title"]')]
                    .some(e => norm(e.textContent) === optionText);
                }""", option)
            if applied:
                self.log.info("已应用排序：%s（选项「%s」，触发器已变更为该值）", sort, option)
            else:
                self.log.warning("排序「%s」已派发但触发器未变化，按默认顺序采集", sort)
        except Exception as exc:
            self.log.warning("排序「%s」应用失败：%s，按默认顺序采集", sort, exc)

    def _take_payload(self, buffer: list[dict]) -> dict | None:
        """取最新一份未消费的搜索主数据响应（含 itemId、体积最大的优先）。"""
        candidates = [
            (i, e) for i, e in enumerate(buffer)
            if not e["consumed"]
            and "idlemtopsearch.pc.search" in e["url"]
            and "shade" not in e["url"] and "activate" not in e["url"]
            and "itemId" in e["body"]
        ]
        if not candidates:
            return None
        idx, entry = max(candidates, key=lambda pair: len(pair[1]["body"]))
        entry["consumed"] = True
        return entry

    def _parse_page(self, page, buffer, task, n, crawled_at, raw_dir):
        """取响应 → 落盘 → 解析；整页失败重试 1 次（T04 任务 4）。"""
        for attempt in (1, 2):
            entry = self._take_payload(buffer)
            if entry is None:
                page.wait_for_timeout(4_000)   # 响应可能晚到，稍候再取
                continue
            (raw_dir / f"page_{n}.json").write_text(entry["body"], encoding="utf-8")
            try:
                return parse_search_payload(json.loads(entry["body"]),
                                            keyword=task.keyword, sort=task.sort,
                                            page_number=n, crawled_at=crawled_at)
            except Exception as exc:
                self.log.warning("「%s」第 %d 页解析失败（第 %d 次尝试）：%s",
                                 task.keyword, n, attempt, exc)
        (raw_dir / f"page_{n}.json").write_text("", encoding="utf-8")  # 占位，预算计数
        return None

    def _goto_page(self, page, n: int) -> bool:
        """点击分页右箭头；轮询激活页码确认翻页成功。无更多结果返回 False。"""
        cur = self._active_page_no(page)
        try:
            clicked = page.evaluate("""() => {
              const arrows = [...document.querySelectorAll('[class*="pagination-arrow-container"]')];
              const right = arrows[arrows.length - 1];
              if (right && !right.hasAttribute('disabled')) { right.click(); return true; }
              return false;
            }""")
        except Exception:
            return False
        if not clicked:
            return False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            page.wait_for_timeout(1_500)
            if self._active_page_no(page) == n and n != cur:
                return True
        return False

    @staticmethod
    def _active_page_no(page) -> int:
        try:
            text = page.evaluate(
                '() => document.querySelector("[class*=page-box-active]")'
                "?.textContent?.trim()")
            return int(text) if text and text.isdigit() else 0
        except Exception:
            return 0

    def _save_manifest(self, buffer: list[dict], raw_dir: Path) -> None:
        try:
            manifest = [{"url": e["url"][:300], "size": len(e["body"]),
                         "consumed": e["consumed"]} for e in buffer]
            (raw_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

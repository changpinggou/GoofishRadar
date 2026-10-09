"""每日任务编排（T08）：会话检查 → 采集 → 入库 → 评分 → 报告 的完整链路。

失败语义（T08 退出码约定）：
- 单关键词失败不中断整体，失败清单进汇总结果；
- 风控中止（RiskBlockedError）时已成功关键词的数据照常入库并出报告（标注 partial）；
- 采集与会话检查通过 SessionManager/异常类型区分退出码：1 会话失效 / 2 采集失败 / 3 报告失败。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from .collector import KeywordRunResult, SearchCollector
from .config import Settings, enabled_tasks
from .logsetup import get_logger
from .report import generate_report
from .ranking import rankings_to_dict, score_day_from_store
from .session import SessionExpiredError, SessionManager
from .storage import Store


@dataclass
class RunSummary:
    date: str
    results: list[KeywordRunResult] = field(default_factory=list)
    aborted_by_risk: bool = False
    session_invalid: bool = False
    report_failed: bool = False
    error: str | None = None
    report: dict | None = None
    started_at: str = ""

    @property
    def ok_keywords(self) -> list[KeywordRunResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed_keywords(self) -> list[KeywordRunResult]:
        return [r for r in self.results if not r.ok]

    @property
    def total_items(self) -> int:
        return sum(len(r.items) for r in self.results)

    def exit_code(self) -> int:
        if self.session_invalid:
            return 1
        if self.report_failed:
            return 3
        if self.failed_keywords or self.aborted_by_risk:
            return 2
        return 0


def run_daily(settings: Settings, keyword_filter: list[str] | None = None,
              skip_session_check: bool = False, log=None) -> RunSummary:
    """执行一次完整的每日任务并返回汇总（exit code 由 RunSummary.exit_code() 决定）。"""
    log = log or get_logger()
    today = datetime.now().strftime("%Y-%m-%d")
    summary = RunSummary(date=today,
                         started_at=datetime.now().isoformat(timespec="seconds"))
    settings.paths.ensure()
    store = Store(settings.paths.data_dir / "goofishradar.db")

    # ---- 1. 会话检查（T02 模块；失效即终止，退出码 1）----
    if not skip_session_check:
        mgr = SessionManager(user_data_dir=settings.paths.user_data_dir,
                             marker_path=settings.paths.session_expired_marker)
        verdict = mgr.check(headless=False)
        if not verdict.valid:
            summary.session_invalid = True
            summary.error = f"会话检查未通过：{verdict.reason}"
            log.error("%s —— 请运行 goofishradar login 重新扫码", summary.error)
            store.close()
            return summary
        log.info("会话有效")

    # ---- 2. 采集（单关键词失败隔离；风控中止则收尾）----
    tasks, warnings = load_keyword_tasks(settings)
    for w in warnings:
        log.warning("配置：%s", w)
    if keyword_filter:
        tasks = [t for t in tasks if t.keyword in set(keyword_filter)]
    if not tasks:
        summary.error = "没有匹配的采集任务"
        log.error(summary.error)
        store.close()
        return summary

    collector = SearchCollector(settings)
    results, aborted = collector.collect_keywords(tasks)
    summary.results = results
    summary.aborted_by_risk = aborted

    # ---- 3. 入库（含失败关键词的 run 记录 —— T05 crawl_run 接线）----
    for r in results:
        if r.ok and r.items:
            stats = store.upsert_items(r.items, snapshot_date=today)
            store.record_run(
                today, r.keyword, sort="", pages_attempted=r.pages_attempted,
                pages_ok=r.pages_ok, pages_degraded=r.degraded_pages,
                items_new=stats.items_new, items_updated=stats.items_updated,
                items_total=len(r.items),
                missing_field_count=r.missing_field_count,
                duration_seconds=r.duration_seconds,
                result="ok")
            log.info("「%s」入库：新增 %d / 更新 %d（累计 %d 条，耗时 %ss）",
                     r.keyword, stats.items_new, stats.items_updated,
                     len(r.items), r.duration_seconds)
        else:
            store.record_run(
                today, r.keyword, pages_attempted=r.pages_attempted,
                pages_ok=r.pages_ok, pages_degraded=r.degraded_pages,
                duration_seconds=r.duration_seconds,
                result="risk_blocked" if r.error and "risk_blocked" in r.error
                else "failed", error=r.error)

    # ---- 4. 评分 + 报告（采集部分失败也照常出报告，标注数据完整性）----
    try:
        rankings = score_day_from_store(store, today, settings.ranking)
        generate_report(today, store, settings, rankings=rankings)
        summary.report = {
            "pool_size": len(rankings["overall"]),
            "rising_note": rankings["rising_note"],
            "weights_applied": rankings["weights_applied"],
        }
    except Exception as exc:
        summary.report_failed = True
        summary.error = f"报告生成失败：{exc}"
        log.error(summary.error)
    finally:
        store.close()

    # ---- 5. 汇总 ----
    if summary.failed_keywords:
        log.warning("失败关键词 %d 个：%s", len(summary.failed_keywords),
                    ", ".join(f"{r.keyword}({r.error})" for r in summary.failed_keywords))
    log.info("当日汇总：成功 %d 词 / 失败 %d 词 / 采集 %d 条 / 报告%s",
             len(summary.ok_keywords), len(summary.failed_keywords),
             summary.total_items,
             "已生成" if not summary.report_failed else "失败")
    return summary


def load_keyword_tasks(settings: Settings):
    from .config import load_keywords
    tasks, warnings = load_keywords()
    return enabled_tasks(tasks), warnings


def recent_status(store: Store, days: int = 7) -> list[dict]:
    """近 N 天运行概况（status 命令与 T12 监控复用）。"""
    rows = store._conn.execute(
        """SELECT run_date, keyword, result, pages_attempted, pages_ok,
                  items_total, duration_seconds, error
           FROM crawl_run
           WHERE run_date >= date('now', ?)
           ORDER BY run_date DESC, keyword""",
        (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]

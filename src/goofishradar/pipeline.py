"""每日任务编排（T08/T09）：会话检查 → 采集 → 入库 → 评分 → 报告 的完整链路。

T09 增强：断点续采（当日 ok 关键词跳过，--force 重采）、失败重试（指数退避 1min/5min，
风控失败不重试）、连续两天全败写 data/ALERT 告警标记。

失败语义（退出码约定）：
- 单关键词失败不中断整体，失败清单进汇总结果；
- 风控中止（RiskBlockedError）时已成功关键词的数据照常入库并出报告；
- 退出码：1 会话失效 / 2 采集失败（含风控中止）/ 3 报告失败 / 0 成功。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .collector import KeywordRunResult, SearchCollector
from .config import Settings, enabled_tasks
from .logsetup import get_logger
from .paths import DATA_DIR
from .report import generate_report
from .ranking import score_day_from_store
from .session import SessionManager
from .storage import Store

_RETRY_BACKOFF_DEFAULT = (60.0, 300.0)   # 指数退避：1 分钟 / 5 分钟（T09 任务 4）


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


def load_keyword_tasks(settings: Settings):
    from .config import load_keywords
    tasks, warnings = load_keywords()
    return enabled_tasks(tasks), warnings


def _resume_filter(store: Store, tasks: list, today: str, force: bool, log
                   ) -> tuple[list, list[str]]:
    """断点续采（T09 任务 3）：当日已成功关键词默认跳过，--force 重采。"""
    if force:
        return tasks, []
    rows = store._conn.execute(
        "SELECT DISTINCT keyword FROM crawl_run WHERE run_date=? AND result='ok'",
        (today,)).fetchall()
    done = {r[0] for r in rows}
    remaining = [t for t in tasks if t.keyword not in done]
    skipped = [t.keyword for t in tasks if t.keyword in done]
    if skipped:
        log.info("断点续采：跳过今日已完成 %s（--force 可重采）", skipped)
    return remaining, skipped


def _is_risk_error(error: str | None) -> bool:
    return bool(error) and "risk_blocked" in error


def run_daily(settings: Settings, keyword_filter: list[str] | None = None,
              skip_session_check: bool = False, force: bool = False,
              retry_backoff_seconds: tuple[float, ...] = _RETRY_BACKOFF_DEFAULT,
              log=None) -> RunSummary:
    """执行一次完整的每日任务并返回汇总（exit code 由 RunSummary.exit_code() 决定）。"""
    log = log or get_logger()
    today = datetime.now().strftime("%Y-%m-%d")
    summary = RunSummary(date=today,
                         started_at=datetime.now().isoformat(timespec="seconds"))
    settings.paths.ensure()
    store = Store(settings.paths.data_dir / "goofishradar.db")

    try:
        # ---- 1. 会话检查（T02 模块；失效即终止，退出码 1）----
        if not skip_session_check:
            mgr = SessionManager(user_data_dir=settings.paths.user_data_dir,
                                 marker_path=settings.paths.session_expired_marker)
            verdict = mgr.check(headless=False)
            if not verdict.valid:
                summary.session_invalid = True
                summary.error = f"会话检查未通过：{verdict.reason}"
                log.error("%s —— 请运行 goofishradar login 重新扫码", summary.error)
                return summary
            log.info("会话有效")

        # ---- 2. 任务加载 + 断点续采（T09 任务 3）----
        tasks, warnings = load_keyword_tasks(settings)
        for w in warnings:
            log.warning("配置：%s", w)
        if keyword_filter:
            tasks = [t for t in tasks if t.keyword in set(keyword_filter)]
        tasks, skipped = _resume_filter(store, tasks, today, force, log)
        if not tasks:
            summary.error = "没有需要执行的采集任务（可能当日已全部完成）"
            log.info(summary.error)
            summary.report = {"skipped_as_done": skipped}
            return summary

        collector = SearchCollector(settings)
        results, aborted = collector.collect_keywords(tasks)

        # ---- 失败重试（T09 任务 4）：非风控失败按退避序列重试，结果按词合并 ----
        for attempt, backoff in enumerate(retry_backoff_seconds, 1):
            if aborted:
                break
            failing = [r.keyword for r in results
                       if not r.ok and not _is_risk_error(r.error)]
            if not failing:
                break
            log.warning("%d 个关键词失败（%s），%.0f 秒后第 %d/%d 次重试",
                        len(failing), ", ".join(failing), backoff, attempt,
                        len(retry_backoff_seconds))
            time.sleep(backoff)
            if aborted:
                break
            retry_tasks = [t for t in tasks if t.keyword in set(failing)]
            retry_results, aborted = collector.collect_keywords(retry_tasks)
            by_kw = {r.keyword: r for r in results}
            for r in retry_results:
                by_kw[r.keyword] = r
            results = list(by_kw.values())

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
                    result="risk_blocked" if _is_risk_error(r.error) else "failed",
                    error=r.error)

        # ---- 连续失败告警（T09 任务 5：连续 2 天全败 → data/ALERT + 日志）----
        _consecutive_failure_alert(store, today, log)

        # ---- 4. 评分 + 报告（采集部分失败也照常出报告）----
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

        # ---- 5. 汇总 ----
        if summary.failed_keywords:
            log.warning("失败关键词 %d 个：%s", len(summary.failed_keywords),
                        ", ".join(f"{r.keyword}({r.error})"
                                  for r in summary.failed_keywords))
        log.info("当日汇总：成功 %d 词 / 失败 %d 词 / 采集 %d 条 / 报告%s",
                 len(summary.ok_keywords), len(summary.failed_keywords),
                 summary.total_items,
                 "已生成" if not summary.report_failed else "失败")
        return summary
    finally:
        store.close()


def _consecutive_failure_alert(store: Store, today: str, log) -> bool:
    """连续 2 天运行记录全部失败（无任何 ok）→ 写 data/ALERT + ERROR 日志。

    P0 为本地标记；P1（T12）升级为邮件/Webhook 推送。
    """
    rows = store._conn.execute(
        """SELECT run_date,
                  COUNT(*) AS n,
                  SUM(CASE WHEN result='ok' THEN 1 ELSE 0 END) AS ok
           FROM crawl_run GROUP BY run_date ORDER BY run_date DESC LIMIT 2"""
    ).fetchall()
    if len(rows) < 2 or rows[0]["run_date"] != today:
        return False
    yesterday = (datetime.fromisoformat(today) - timedelta(days=1)).strftime("%Y-%m-%d")
    if rows[1]["run_date"] != yesterday or rows[1]["ok"] or rows[0]["ok"]:
        return False
    marker = Path(DATA_DIR) / "ALERT"
    marker.write_text(
        f"{datetime.now().isoformat(timespec='seconds')} ALERT: "
        f"{yesterday} 与 {today} 连续两天任务全部失败，请检查会话/风控状态\n",
        encoding="utf-8")
    log.error("ALERT: 连续两天任务全部失败（%s、%s），已置标记 %s",
              yesterday, today, marker)
    return True


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

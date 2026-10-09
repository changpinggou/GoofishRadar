"""T08 单元测试：pipeline 入库接线（crawl_run）/ 汇总与退出码 / status / report 重出。

采集器被 mock（离线），真实全链路由 T08 实况联调覆盖。
"""
from __future__ import annotations

import json

import pytest

from goofishradar import pipeline
from goofishradar.collector import KeywordRunResult
from goofishradar.config import PathsCfg, Settings
from goofishradar.parser import ItemRecord
from goofishradar.pipeline import RunSummary, run_daily
from goofishradar.report import generate_report
from goofishradar.storage import Store


def make_record(item_id: str, want: int, keyword: str, crawled_at: str) -> ItemRecord:
    return ItemRecord(
        item_id=item_id, title=f"商品{item_id}", price=100.0, want_count=want,
        view_count=None, publish_time=None, status="在售", seller_nick="s",
        seller_id="1", location="上海", pic_url="p",
        item_url=f"https://www.goofish.com/item?id={item_id}", tags=[],
        source_keyword=keyword, source_sort="most_want", page_number=1,
        crawled_at=crawled_at)


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(paths=PathsCfg(
        data_dir=tmp_path / "data", reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs"))
    settings.paths.ensure()

    def fake_collect(self, tasks):
        out = []
        for i, t in enumerate(tasks):
            out.append(KeywordRunResult(
                keyword=t.keyword, pages_attempted=2, pages_ok=2,
                items=[make_record(f"{t.keyword}-{n}", want=10 + n,
                                   keyword=t.keyword,
                                   crawled_at="2026-10-09 08:00:00")
                       for n in range(3)],
                duration_seconds=5.0 + i))
        return out, False

    monkeypatch.setattr(pipeline.SearchCollector, "collect_keywords", fake_collect)
    return settings


def two_task_settings(settings):
    # pipeline.load_keyword_tasks 读取仓库 keywords.yaml，这里 monkeypatch 成两个词
    from goofishradar.config import KeywordTask
    orig = pipeline.load_keyword_tasks
    pipeline.load_keyword_tasks = lambda s: (
        [KeywordTask(keyword="词A"), KeywordTask(keyword="词B")], [])
    return orig


class TestRunDaily:
    def test_full_chain_writes_store_and_report(self, env):
        settings = env
        two_task_settings(settings)
        summary = run_daily(settings, skip_session_check=True)
        store = Store(settings.paths.data_dir / "goofishradar.db")
        try:
            counts = store.counts()
            # 采集 2 词 × 3 条，条目 id 带关键词前缀不冲突 → 6 商品
            assert counts["items"] == 6 and counts["snapshots"] == 6
            assert counts["runs"] == 2
            rows = pipeline.recent_status(store, days=7)
            assert len(rows) == 2
            assert all(r["result"] == "ok" for r in rows)
            assert rows[0]["items_total"] == 3
            assert rows[0]["duration_seconds"] is not None     # T04 duration 接线
            report_dir = settings.paths.reports_dir / summary.date
            assert (report_dir / "report.html").exists()
            assert (report_dir / "rankings.xlsx").exists()
        finally:
            store.close()
        assert summary.exit_code() == 0
        assert summary.failed_keywords == []
        # 评分归档存在（次日对比链）
        assert (settings.paths.data_dir / "parsed" / summary.date
                / "_rankings.json").exists()

    def test_failed_keyword_isolated_exit_2(self, env, monkeypatch):
        settings = env
        two_task_settings(settings)

        def fake_collect(self, tasks):
            ok = KeywordRunResult(
                keyword="词A", pages_attempted=2, pages_ok=2,
                items=[make_record("A-1", 5, "词A", "2026-10-09 08:00:00")],
                duration_seconds=3.0)
            bad = KeywordRunResult(keyword="词B", pages_attempted=2, pages_ok=0,
                                   error="timeout")
            return [ok, bad], False

        monkeypatch.setattr(pipeline.SearchCollector, "collect_keywords", fake_collect)
        summary = run_daily(settings, skip_session_check=True)
        assert summary.exit_code() == 2
        assert len(summary.ok_keywords) == 1 and len(summary.failed_keywords) == 1
        store = Store(settings.paths.data_dir / "goofishradar.db")
        try:
            rows = pipeline.recent_status(store)
            results = {r["keyword"]: r["result"] for r in rows}
            assert results == {"词A": "ok", "词B": "failed"}
            assert store.counts()["items"] == 1          # 成功词照常入库
            assert (settings.paths.reports_dir / summary.date
                    / "report.html").exists()            # 报告照常产出
        finally:
            store.close()

    def test_risk_abort_exit_2_with_partial_data(self, env, monkeypatch):
        settings = env
        two_task_settings(settings)

        def fake_collect(self, tasks):
            return [KeywordRunResult(keyword="词A", error="risk_blocked: 验证码",
                                     pages_attempted=1)], True

        monkeypatch.setattr(pipeline.SearchCollector, "collect_keywords", fake_collect)
        summary = run_daily(settings, skip_session_check=True)
        assert summary.aborted_by_risk is True
        assert summary.exit_code() == 2
        store = Store(settings.paths.data_dir / "goofishradar.db")
        try:
            rows = pipeline.recent_status(store)
            assert rows[0]["result"] == "risk_blocked"
        finally:
            store.close()

    def test_keyword_filter(self, env):
        settings = env
        two_task_settings(settings)
        summary = run_daily(settings, keyword_filter=["词A"], skip_session_check=True)
        assert len(summary.results) == 1
        assert summary.results[0].keyword == "词A"


class TestRecentStatus:
    def test_format_fields(self, tmp_path):
        store = Store(tmp_path / "s.db")
        store.record_run("2026-10-09", "iPhone", pages_attempted=3, pages_ok=2,
                         items_total=60, duration_seconds=90.5, result="ok")
        rows = pipeline.recent_status(store, days=7)
        assert rows[0]["keyword"] == "iPhone"
        assert rows[0]["items_total"] == 60
        store.close()


class TestReportCommand:
    def test_report_regenerates_from_existing_db(self, env):
        settings = env
        two_task_settings(settings)
        run_daily(settings, skip_session_check=True)

        store = Store(settings.paths.data_dir / "goofishradar.db")
        out = generate_report(summary_date(settings), store, settings)
        store.close()
        assert out["html"].exists() and out["html"].stat().st_size > 1000


def summary_date(settings) -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")

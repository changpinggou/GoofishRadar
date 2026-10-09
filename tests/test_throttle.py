"""T11 单元测试：全局节流真实时序 / 请求审计与间隔 / 日预算硬顶 / 风控冷却 / mock 验证码中止。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from goofishradar import pipeline
from goofishradar.collector import KeywordRunResult, SearchCollector
from goofishradar.config import KeywordTask, PathsCfg, Settings
from goofishradar.session import SessionState, SessionVerdict
from goofishradar.storage import Store
from goofishradar.throttle import (
    DailyBudget, GlobalThrottle, RateLimiter, RequestAuditor, RiskControl)


# ---- 全局节流（T11 验收标准 1：相邻请求间隔 ≥ 配置下限，真实时序测量）----

class TestGlobalThrottle:
    def test_two_requests_gap_at_least_lower_bound(self):
        throttle = GlobalThrottle(0.2, 0.35)
        start = time.monotonic()
        throttle.wait()                       # 首次立即放行
        first = time.monotonic() - start
        assert first < 0.1
        throttle.wait()                       # 第二次强制距首次 ≥ 0.2s
        total = time.monotonic() - start
        assert total >= 0.2

    def test_cross_instance_gap_enforced(self):
        """跨关键词场景：同一节流器实例的放行序列整体满足间隔（模拟全局使用）。"""
        throttle = GlobalThrottle(0.05, 0.05)  # 固定 50ms 便于断言
        stamps = [time.monotonic()]
        for _ in range(5):
            throttle.wait()
            stamps.append(time.monotonic())
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert all(g >= 0.045 for g in gaps[1:])   # 首次放行不计入，其后全部 ≥50ms

    def test_rate_limiter_bounds_preserved(self):
        rate = RateLimiter(8, 20)
        assert all(8 <= rate.next_delay() <= 20 for _ in range(100))


# ---- 请求审计（间隔统计 = 验收标准 1 的日志证据源）----

class TestRequestAuditor:
    def test_record_count_and_gaps(self, tmp_path):
        audit = RequestAuditor(tmp_path / "logs")
        base = datetime.now()
        for i, url in enumerate(["u1", "u2", "u3"]):
            audit.record_at(url, base + timedelta(seconds=[0, 9, 25][i]))
        assert audit.count_today() == 3
        gaps = audit.gaps_seconds()
        assert gaps == [9.0, 16.0]
        # 最低间隔统计（验收标准 1 证据口径）
        assert min(gaps) >= 9.0

    def test_day_files_isolated(self, tmp_path):
        audit = RequestAuditor(tmp_path / "logs")
        audit.record_at("y", datetime(2026, 10, 1, 8, 0))
        audit.record("t")
        assert audit.count_today() == 1
        assert len(audit.gaps_seconds()) == 0            # 跨日不混算


# ---- 单日预算硬顶（T11 验收标准 2：硬顶不可被配置突破）----

class TestDailyBudget:
    def test_budget_hard_cap_not_exceedable(self, tmp_path):
        audit = RequestAuditor(tmp_path / "logs")
        for i in range(299):
            audit.record_at(f"u{i}", datetime.now())
        budget = DailyBudget(audit, max_pages=300)
        assert budget.used() == 299
        assert budget.remaining() == 1                    # 仅剩 1 页

    def test_stress_config_over_limit_clamped_in_code(self, tmp_path):
        """压测记录：配置 99999 页/日，LimitsCfg 截断为 300（代码层硬顶）。"""
        from goofishradar.config import (
            HARD_MAX_PAGES_PER_DAY, LimitsCfg, load_settings)
        # 直接构造（代码硬顶常量）
        cfg = LimitsCfg(max_pages_per_day=99999)
        assert HARD_MAX_PAGES_PER_DAY == 300
        # 配置加载截断
        cfg_file = tmp_path / "settings.yaml"
        cfg_file.write_text(
            "limits:\n  max_pages_per_day: 99999\n"
            "  max_pages_per_keyword: 999\n"
            "  page_delay_seconds: [8, 20]\n", encoding="utf-8")
        settings, warnings = load_settings(cfg_file)
        assert settings.limits.max_pages_per_day == 300
        assert settings.limits.max_pages_per_keyword == 50
        assert any("已自动截断" in w for w in warnings)

    def test_collector_budget_exhausted_skip(self, tmp_path):
        settings = Settings(paths=PathsCfg(
            data_dir=tmp_path / "d", reports_dir=tmp_path / "r",
            logs_dir=tmp_path / "l"))
        settings.paths.ensure()
        collector = SearchCollector(settings)
        for i in range(300):
            collector._audit.record_at(f"u{i}", datetime.now())
        res = collector.collect_keyword.__wrapped__ if False else None
        result = collector._page_budget(KeywordTask(keyword="x", max_pages=3))
        assert result == 0
        # 直接走 collect_keyword 的预算用尽分支
        out = collector.collect_keyword(ctx=None, task=KeywordTask(keyword="x"),
                                        idx=1, total=1)
        assert out.error == "daily_budget_exhausted"


# ---- 风控冷却档位 ----

class TestRiskControl:
    def test_trigger_and_expire(self, tmp_path):
        rc = RiskControl(tmp_path, cooldown_minutes=120)
        assert rc.is_active() is False
        until = rc.trigger("验证码")
        assert until > datetime.now() + timedelta(minutes=119)
        assert rc.is_active() is True
        assert "验证码" in rc.marker_path.read_text(encoding="utf-8")
        # 过期标记 → 自动清除
        rc.marker_path.write_text(
            (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"),
            encoding="utf-8")
        assert rc.active_until() is None and rc.is_active() is False
        assert not rc.marker_path.exists()

    def test_clear(self, tmp_path):
        rc = RiskControl(tmp_path, 60)
        rc.trigger("x")
        rc.clear()
        assert rc.is_active() is False


# ---- mock 验证码页面：立即中止（T11 验收标准 3）----

class TestMockCaptchaAbort:
    class FakePage:
        def __init__(self, text):
            self._text = text

        def evaluate(self, _js):
            return self._text

    def test_check_risk_raises_on_captcha_text(self, tmp_path):
        settings = Settings(paths=PathsCfg(
            data_dir=tmp_path / "d", reports_dir=tmp_path / "r",
            logs_dir=tmp_path / "l"))
        settings.paths.ensure()
        collector = SearchCollector(settings)
        page = self.FakePage("请完成 安全验证 之后继续操作")
        with pytest.raises(pipeline_risk_error()):
            collector._check_risk(page, "iPhone")

    def test_normal_page_does_not_raise(self, tmp_path):
        settings = Settings(paths=PathsCfg(
            data_dir=tmp_path / "d", reports_dir=tmp_path / "r",
            logs_dir=tmp_path / "l"))
        settings.paths.ensure()
        collector = SearchCollector(settings)
        collector._check_risk(self.FakePage("闲鱼 - 闲不住？上闲鱼！"), "iPhone")


def pipeline_risk_error():
    from goofishradar.collector import RiskBlockedError
    return RiskBlockedError


# ---- pipeline 集成：冷却门控 / RISK_BLOCKED→exit 2 + 触发冷却 ----

@pytest.fixture
def settings_env(tmp_path):
    settings = Settings(paths=PathsCfg(
        data_dir=tmp_path / "d", reports_dir=tmp_path / "r",
        logs_dir=tmp_path / "l"))
    settings.paths.ensure()
    return settings


class TestPipelineRiskGating:
    def test_cooldown_blocks_all_network(self, settings_env, monkeypatch):
        rc = RiskControl(settings_env.paths.data_dir, 120)
        rc.trigger("历史拦截")
        called = []
        monkeypatch.setattr(
            pipeline.SearchCollector, "collect_keywords",
            lambda self, tasks: called.append(tasks) or ([], False))
        monkeypatch.setattr(
            pipeline.SessionManager, "check",
            lambda self, headless: SessionVerdict(SessionState.VALID, "x"))
        summary = pipeline.run_daily(settings_env, skip_session_check=False,
                                     retry_backoff_seconds=(0, 0))
        assert called == []                       # 冷却期零网络请求
        assert summary.aborted_by_risk is True
        assert summary.exit_code() == 2
        assert "冷却" in summary.error

    def test_session_check_risk_blocked_triggers_cooldown(self, settings_env,
                                                          monkeypatch):
        monkeypatch.setattr(
            pipeline.SessionManager, "check",
            lambda self, headless: SessionVerdict(
                SessionState.RISK_BLOCKED, "页面触发风控拦截"))
        called = []
        monkeypatch.setattr(
            pipeline.SearchCollector, "collect_keywords",
            lambda self, tasks: called.append(tasks) or ([], False))
        summary = pipeline.run_daily(settings_env, skip_session_check=False,
                                     retry_backoff_seconds=(0, 0))
        assert summary.exit_code() == 2
        assert summary.aborted_by_risk is True
        assert called == []                       # 不进入采集
        rc = RiskControl(settings_env.paths.data_dir)
        assert rc.is_active() is True             # 冷却已被触发

    def test_retry_backoff_from_settings(self, settings_env, monkeypatch):
        settings = settings_env
        # 通过配置注入零退避（不再依赖显式参数）
        settings.limits.retry_backoff_seconds = (0.0, 0.0)
        calls = []

        def fake_collect(self, tasks):
            calls.append(1)
            if len(calls) == 1:
                return ([KeywordRunResult(keyword="iPhone", error="timeout")],
                        False)
            return ([KeywordRunResult(
                keyword="iPhone", pages_attempted=1, pages_ok=1,
                items=[], duration_seconds=1.0)], False)

        monkeypatch.setattr(pipeline.SearchCollector, "collect_keywords", fake_collect)
        monkeypatch.setattr(
            pipeline, "load_keyword_tasks",
            lambda s: ([KeywordTask(keyword="iPhone")], []))
        summary = pipeline.run_daily(settings, skip_session_check=True)  # 不传退避参数
        assert len(calls) == 2 and summary.exit_code() == 0

    def test_report_carries_partial_complete_note(self, settings_env, monkeypatch):
        settings = settings_env

        def fake_collect(self, tasks):
            return ([KeywordRunResult(keyword="iPhone",
                                      error="daily_budget_exhausted")], False)

        monkeypatch.setattr(pipeline.SearchCollector, "collect_keywords", fake_collect)
        monkeypatch.setattr(
            pipeline, "load_keyword_tasks",
            lambda s: ([KeywordTask(keyword="iPhone")], []))
        summary = pipeline.run_daily(settings, skip_session_check=True,
                                     retry_backoff_seconds=(0, 0))
        assert summary.report is not None
        report_dir = settings.paths.reports_dir / summary.date
        html = (report_dir / "report.html").read_text(encoding="utf-8")
        assert "单日页数预算用尽" in html and "部分完成" in html

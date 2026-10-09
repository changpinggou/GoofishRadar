"""T09 单元测试：plist 生成/装卸、错峰计算、断点续采、失败重试、连续失败告警。"""
from __future__ import annotations

import plistlib
from datetime import datetime, timedelta

import pytest

from goofishradar import pipeline
from goofishradar.collector import KeywordRunResult
from goofishradar.config import KeywordTask, PathsCfg, RankingCfg, ScheduleCfg, Settings
from goofishradar.parser import ItemRecord
from goofishradar.scheduler import (
    build_plist, earliest_fire, install_schedule, uninstall_schedule)
from goofishradar.storage import Store
from goofishradar._daily import compute_run_at


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """隔离全局 data/ 目录（ALERT 标记等），测试不触碰真实数据。"""
    fake = tmp_path / "globaldata"
    fake.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pipeline, "DATA_DIR", fake)
    return fake


def make_settings(tmp_path, **schedule) -> Settings:
    return Settings(
        schedule=ScheduleCfg(**schedule) if schedule else ScheduleCfg(),
        ranking=RankingCfg(),
        paths=PathsCfg(data_dir=tmp_path / "data",
                       reports_dir=tmp_path / "reports",
                       logs_dir=tmp_path / "logs"))


def make_record(item_id: str, want: int, keyword: str) -> ItemRecord:
    return ItemRecord(
        item_id=item_id, title=f"商品{item_id}", price=100.0, want_count=want,
        view_count=None, publish_time=None, status="在售", seller_nick="s",
        seller_id="1", location="上海", pic_url="p",
        item_url=f"https://www.goofish.com/item?id={item_id}", tags=[],
        source_keyword=keyword, source_sort="most_want", page_number=1,
        crawled_at="2026-10-09 08:00:00")


@pytest.fixture
def settings_env(tmp_path):
    settings = make_settings(tmp_path)
    settings.paths.ensure()
    return settings


def _ok(keyword, want=10):
    return KeywordRunResult(
        keyword=keyword, pages_attempted=2, pages_ok=2,
        items=[make_record(f"{keyword}-{want}", want, keyword)],
        duration_seconds=1.0)


def _patch_collect(monkeypatch, calls_box, behaviors):
    """behaviors: 每次 collect_keywords 调用依次返回的 (results, aborted)。"""
    def fake(self, tasks):
        calls_box.append([t.keyword for t in tasks])
        idx = min(len(calls_box) - 1, len(behaviors) - 1)
        return behaviors[idx]

    monkeypatch.setattr(pipeline.SearchCollector, "collect_keywords", fake)


def _patch_tasks(monkeypatch, keywords):
    monkeypatch.setattr(
        pipeline, "load_keyword_tasks",
        lambda s: ([KeywordTask(keyword=k) for k in keywords], []))


# ---- plist 生成与装卸 ----

class TestScheduler:
    def test_plist_content_matches_settings(self, tmp_path):
        settings = make_settings(tmp_path, time="08:00", randomize_minutes=15)
        data = plistlib.loads(build_plist(settings, tmp_path / "py", tmp_path))
        assert data["Label"] == "com.goofishradar.daily"
        assert data["StartCalendarInterval"] == {"Hour": 8, "Minute": 0}
        assert data["ProgramArguments"][-1] == "goofishradar._daily"
        assert data["ProgramArguments"][0].endswith("py")
        assert data["WorkingDirectory"] == str(tmp_path)
        assert data["StandardOutPath"].endswith("launchd_daily.log")
        assert data["EnvironmentVariables"]["PYTHONPATH"].endswith("src")

    def test_earliest_fire_picks_min_of_weekday_weekend(self, tmp_path):
        settings = make_settings(tmp_path, time="09:30",
                                 weekday_time="08:00", weekend_time="09:30")
        assert earliest_fire(settings) == (8, 0)

    def test_install_uninstall_roundtrip_without_launchctl(self, tmp_path):
        settings = make_settings(tmp_path)
        plist_file = tmp_path / "Library" / "LaunchAgents" / "x.plist"
        install_schedule(settings, python_bin=tmp_path / "py", root=tmp_path,
                         plist_file=plist_file, load=False)
        assert plist_file.exists()
        install_schedule(settings, python_bin=tmp_path / "py", root=tmp_path,
                         plist_file=plist_file, load=False)   # 幂等
        assert plist_file.exists()
        assert uninstall_schedule(plist_file, load=False) is True
        assert not plist_file.exists()
        assert uninstall_schedule(plist_file, load=False) is False


# ---- 错峰计算（T09 任务 2）----

class TestComputeRunAt:
    def test_weekday_uses_weekday_time(self, tmp_path):
        settings = make_settings(tmp_path, time="08:00", randomize_minutes=0)
        now = datetime(2026, 10, 9, 7, 0)                # 周五
        assert compute_run_at(now, settings) == datetime(2026, 10, 9, 8, 0)

    def test_weekend_uses_weekend_time(self, tmp_path):
        settings = make_settings(tmp_path, time="08:00", weekend_time="09:30",
                                 randomize_minutes=0)
        now = datetime(2026, 10, 10, 7, 0)               # 周六
        assert compute_run_at(now, settings) == datetime(2026, 10, 10, 9, 30)

    def test_jitter_within_window(self, tmp_path):
        settings = make_settings(tmp_path, time="08:00", randomize_minutes=15)
        now = datetime(2026, 10, 9, 7, 0)
        for _ in range(50):
            delta = (compute_run_at(now, settings) - now).total_seconds() / 60
            assert 45 - 0.01 <= delta <= 75 + 0.01       # 08:00 ± 15 分钟

    def test_past_target_clamped_to_near_future(self, tmp_path):
        settings = make_settings(tmp_path, time="08:00", randomize_minutes=15)
        now = datetime(2026, 10, 9, 9, 0)                # 已过目标时刻
        run_at = compute_run_at(now, settings)
        assert now <= run_at <= now + timedelta(seconds=90)


# ---- 断点续采 / 重试 / 告警 ----

class TestResume:
    def test_done_keywords_skipped_today(self, settings_env, monkeypatch):
        settings = settings_env
        store = Store(settings.paths.data_dir / "goofishradar.db")
        today = datetime.now().strftime("%Y-%m-%d")
        store.record_run(today, "iPhone", pages_attempted=1, pages_ok=1,
                         items_total=10, result="ok")
        store.close()

        calls = []
        _patch_collect(monkeypatch, calls,
                       [([_ok("乐高"), _ok("佳能相机")], False)])
        _patch_tasks(monkeypatch, ["iPhone", "乐高", "佳能相机"])
        summary = pipeline.run_daily(settings, skip_session_check=True,
                                     retry_backoff_seconds=(0, 0))

        assert calls == [["乐高", "佳能相机"]]            # iPhone 被跳过
        assert summary.exit_code() == 0
        # 跳过词的 crawl_run 仍在（未重复记录今日 ok → 仍只有 1 条 iPhone 记录）
        store = Store(settings.paths.data_dir / "goofishradar.db")
        try:
            rows = [r for r in pipeline.recent_status(store)
                    if r["keyword"] == "iPhone"]
            assert len(rows) == 1
        finally:
            store.close()

    def test_force_recollects_done_keywords(self, settings_env, monkeypatch):
        settings = settings_env
        store = Store(settings.paths.data_dir / "goofishradar.db")
        today = datetime.now().strftime("%Y-%m-%d")
        store.record_run(today, "iPhone", pages_attempted=1, pages_ok=1,
                         result="ok")
        store.close()

        calls = []
        _patch_collect(monkeypatch, calls, [([_ok("iPhone")], False)])
        _patch_tasks(monkeypatch, ["iPhone"])
        summary = pipeline.run_daily(settings, skip_session_check=True,
                                     force=True, retry_backoff_seconds=(0, 0))
        assert calls == [["iPhone"]]
        assert summary.exit_code() == 0


class TestRetry:
    def test_failed_keyword_retried_then_merged(self, settings_env, monkeypatch):
        settings = settings_env
        calls = []
        behaviors = [
            ([_ok("乐高"),
              KeywordRunResult(keyword="iPhone", pages_attempted=2, pages_ok=0,
                               error="timeout")], False),
            ([_ok("iPhone", want=7)], False),          # 重试成功
        ]
        _patch_collect(monkeypatch, calls, behaviors)
        _patch_tasks(monkeypatch, ["iPhone", "乐高"])
        summary = pipeline.run_daily(settings, skip_session_check=True,
                                     retry_backoff_seconds=(0, 0))

        assert len(calls) == 2
        assert calls[1] == ["iPhone"]                     # 只重试失败词
        assert summary.exit_code() == 0
        assert [r.keyword for r in summary.ok_keywords] == ["乐高", "iPhone"]
        assert summary.ok_keywords[1].items[0].want_count == 7   # 重试结果已合并
        store = Store(settings.paths.data_dir / "goofishradar.db")
        try:
            results = {r["keyword"]: r["result"]
                       for r in pipeline.recent_status(store)}
            assert results == {"iPhone": "ok", "乐高": "ok"}
        finally:
            store.close()

    def test_risk_blocked_not_retried(self, settings_env, monkeypatch):
        settings = settings_env
        calls = []
        _patch_collect(monkeypatch, calls,
                       [([KeywordRunResult(keyword="iPhone",
                                           error="risk_blocked: 验证码")], True)])
        _patch_tasks(monkeypatch, ["iPhone"])
        summary = pipeline.run_daily(settings, skip_session_check=True,
                                     retry_backoff_seconds=(0, 0))
        assert len(calls) == 1                            # 风控不重试
        assert summary.exit_code() == 2


class TestConsecutiveFailureAlert:
    def test_two_consecutive_failed_days_trigger_alert(
            self, settings_env, isolated_data_dir, monkeypatch):
        settings = settings_env
        store = Store(settings.paths.data_dir / "goofishradar.db")
        store.record_run("2026-10-08", "iPhone", pages_attempted=1, pages_ok=0,
                         result="failed", error="x")
        store.close()

        calls = []
        _patch_collect(monkeypatch, calls,
                       [([KeywordRunResult(keyword="iPhone", error="timeout")],
                         False)])
        _patch_tasks(monkeypatch, ["iPhone"])
        pipeline.run_daily(settings, skip_session_check=True,
                           retry_backoff_seconds=(0, 0))

        assert (isolated_data_dir / "ALERT").exists()

    def test_success_day_does_not_alert(self, settings_env, isolated_data_dir,
                                        monkeypatch):
        settings = settings_env
        store = Store(settings.paths.data_dir / "goofishradar.db")
        store.record_run("2026-10-08", "iPhone", pages_attempted=1, pages_ok=0,
                         result="failed", error="x")
        store.close()

        calls = []
        _patch_collect(monkeypatch, calls, [([_ok("iPhone")], False)])
        _patch_tasks(monkeypatch, ["iPhone"])
        pipeline.run_daily(settings, skip_session_check=True,
                           retry_backoff_seconds=(0, 0))
        assert not (isolated_data_dir / "ALERT").exists()

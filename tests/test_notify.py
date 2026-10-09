"""T12 单元测试：Notify 渠道 payload/降级/回退、pipeline 四类告警场景、
status 与 crawl_run 一致性、事件标记与手机号脱敏。"""
from __future__ import annotations

import base64
import io
import json
import logging

import pytest

from goofishradar import pipeline
from goofishradar.collector import KeywordRunResult
from goofishradar.config import KeywordTask, NotifyCfg, PathsCfg, Settings
from goofishradar.logsetup import log_event, redact
from goofishradar.notify import (
    Alert, SmtpNotifier, WebhookNotifier, build_notifiers, send_alerts)
from goofishradar.parser import ItemRecord
from goofishradar.storage import Store


def make_settings(tmp_path, notify: NotifyCfg | None = None) -> Settings:
    s = Settings(paths=PathsCfg(data_dir=tmp_path / "d",
                                reports_dir=tmp_path / "r",
                                logs_dir=tmp_path / "l"),
                 notify=notify or NotifyCfg())
    s.paths.ensure()
    return s


def ok(keyword, want=10):
    return KeywordRunResult(
        keyword=keyword, pages_attempted=2, pages_ok=2,
        items=[ItemRecord(
            item_id=f"{keyword}-{want}", title="t", price=1.0, want_count=want,
            view_count=None, publish_time=None, status="在售", seller_nick="s",
            seller_id="1", location="x", pic_url="p",
            item_url=f"u{keyword}", tags=[], source_keyword=keyword,
            source_sort="", page_number=1, crawled_at="2026-10-09 08:00:00")],
        duration_seconds=1.0)


# ---- 渠道 payload 与构建 ----

class TestChannels:
    def test_webhook_payload_feishu_and_dingtalk(self):
        alert = Alert("critical", "session_expired", "会话失效", "2026-10-09")
        feishu = WebhookNotifier("http://x", "feishu").payload(alert)
        assert feishu == {"msg_type": "text",
                          "content": {"text": alert.render()}}
        ding = WebhookNotifier("http://x", "dingtalk").payload(alert)
        assert ding == {"msgtype": "text", "text": {"content": alert.render()}}

    def test_build_notifiers_by_config(self, tmp_path):
        s = make_settings(tmp_path, NotifyCfg(webhook_url="http://x",
                                              webhook_channel="dingtalk"))
        notifiers = build_notifiers(s)
        assert len(notifiers) == 1 and notifiers[0].channel == "dingtalk"

        s2 = make_settings(tmp_path, NotifyCfg(smtp_host="smtp.x.com",
                                               smtp_to="a@b.c"))
        notifiers2 = build_notifiers(s2)
        assert len(notifiers2) == 1 and notifiers2[0].name == "smtp"

    def test_smtp_send_via_mock(self, tmp_path, monkeypatch):
        sent = {}
        class FakeSMTP:
            def __init__(self, host, port, timeout=None): sent["conn"] = (host, port)
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def login(self, u, p): sent["login"] = (u, p)
            def sendmail(self, f, to, raw): sent["mail"] = (f, to, raw)
        monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
        s = SmtpNotifier("smtp.x.com", 465, "me@x.com", "pw", "to@x.com")
        s.send(Alert("warning", "low_volume", "仅 50 条", "2026-10-09"))
        assert sent["conn"] == ("smtp.x.com", 465)
        raw = sent["mail"][2]
        body = base64.b64decode(raw.split("\n\n", 1)[1]).decode("utf-8")
        assert "low_volume" in body                              # base64 正文解码后含事件名
        assert "to@x.com" in raw


# ---- 发送与降级 ----

class TestSendAlerts:
    def test_unconfigured_degrades_to_local_marker(self, tmp_path):
        s = make_settings(tmp_path)
        result = send_alerts([Alert("warning", "low_volume", "m", "d")], s)
        assert result["degraded"] == 1
        marker = s.paths.data_dir / "ALERT"
        assert marker.exists() and "low_volume" in marker.read_text(encoding="utf-8")

    def test_configured_send_ok_no_marker(self, tmp_path, monkeypatch):
        s = make_settings(tmp_path, NotifyCfg(webhook_url="http://hook"))
        pushed = []
        monkeypatch.setattr(WebhookNotifier, "send",
                            lambda self, alert: pushed.append(alert.event))
        send_alerts([Alert("warning", "low_volume", "m", "d")], s)
        assert pushed == ["low_volume"]
        assert not (s.paths.data_dir / "ALERT").exists()

    def test_send_failure_falls_back_to_marker(self, tmp_path, monkeypatch):
        s = make_settings(tmp_path, NotifyCfg(webhook_url="http://hook"))
        def boom(self, alert):
            raise RuntimeError("网络不可达")
        monkeypatch.setattr(WebhookNotifier, "send", boom)
        result = send_alerts([Alert("critical", "risk_blocked", "m", "d")], s)
        assert "failed" in result["sent"]["webhook:risk_blocked"]
        assert (s.paths.data_dir / "ALERT").exists()     # 降级本地标记


# ---- pipeline 四类告警场景 ----

@pytest.fixture
def env(tmp_path):
    s = make_settings(tmp_path)
    return s


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """隔离全局 data/ 目录（ALERT 等标记），测试不污染真实数据。"""
    fake = tmp_path / "globaldata"
    fake.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pipeline, "DATA_DIR", fake)
    return fake


def _patch_tasks(monkeypatch, keywords):
    monkeypatch.setattr(
        pipeline, "load_keyword_tasks",
        lambda s: ([KeywordTask(keyword=k) for k in keywords], []))


class TestAlertScenarios:
    def test_low_volume_alert(self, env, monkeypatch):
        captured = []
        monkeypatch.setattr(
            pipeline, "send_alerts",
            lambda alerts, s, log=None: captured.extend(alerts))
        _patch_tasks(monkeypatch, ["iPhone"])     # 仅 1 条 < 100
        monkeypatch.setattr(
            pipeline.SearchCollector, "collect_keywords",
            lambda self, tasks: ([ok("iPhone", want=1)], False))
        pipeline.run_daily(env, skip_session_check=True,
                           retry_backoff_seconds=(0, 0))
        events = [a.event for a in captured]
        assert "low_volume" in events

    def test_high_parse_failure_alert(self, env, monkeypatch):
        captured = []
        monkeypatch.setattr(
            pipeline, "send_alerts",
            lambda alerts, s, log=None: captured.extend(alerts))
        _patch_tasks(monkeypatch, ["iPhone"])
        bad = KeywordRunResult(keyword="iPhone", pages_attempted=10, pages_ok=1,
                               degraded_pages=8,                      # 80% > 20%
                               items=[ok("iPhone").items[0]],
                               duration_seconds=1.0)
        monkeypatch.setattr(
            pipeline.SearchCollector, "collect_keywords",
            lambda self, tasks: ([bad], False))
        pipeline.run_daily(env, skip_session_check=True,
                           retry_backoff_seconds=(0, 0))
        assert "high_parse_failure" in [a.event for a in captured]

    def test_session_expired_alert(self, env, monkeypatch):
        captured = []
        monkeypatch.setattr(
            pipeline, "send_alerts",
            lambda alerts, s, log=None: captured.extend(alerts))
        monkeypatch.setattr(
            pipeline.SessionManager, "check",
            lambda self, headless: __import__(
                "goofishradar.session", fromlist=["SessionState"]
            ).SessionVerdict(
                __import__("goofishradar.session",
                           fromlist=["SessionState"]).SessionState.EXPIRED,
                "缺少用户Cookie"))
        summary = pipeline.run_daily(env, skip_session_check=False)
        assert summary.session_invalid is True
        assert "session_expired" in [a.event for a in captured]

    def test_consecutive_failures_alert(self, env, isolated_data_dir, monkeypatch):
        captured = []
        monkeypatch.setattr(
            pipeline, "send_alerts",
            lambda alerts, s, log=None: captured.extend(alerts))
        store = Store(env.paths.data_dir / "goofishradar.db")
        store.record_run("2026-10-08", "iPhone", pages_attempted=1, pages_ok=0,
                         result="failed", error="x")
        store.close()
        _patch_tasks(monkeypatch, ["iPhone"])
        monkeypatch.setattr(
            pipeline.SearchCollector, "collect_keywords",
            lambda self, tasks: ([KeywordRunResult(
                keyword="iPhone", error="timeout")], False))
        pipeline.run_daily(env, skip_session_check=True,
                           retry_backoff_seconds=(0, 0))
        assert "consecutive_failures" in [a.event for a in captured]
        assert (isolated_data_dir / "ALERT").exists()   # 本地标记写入隔离目录


# ---- status 与 crawl_run 一致性（T12 验收标准 2）----

class TestStatusConsistency:
    def test_status_rows_match_crawl_run(self, env):
        store = Store(env.paths.data_dir / "goofishradar.db")
        store.record_run("2026-10-09", "iPhone", pages_attempted=3, pages_ok=3,
                         items_total=90, duration_seconds=60.0, result="ok")
        store.record_run("2026-10-09", "乐高", pages_attempted=2, pages_ok=1,
                         items_total=10, duration_seconds=40.0, result="failed",
                         error="boom")
        rows = pipeline.recent_status(store, days=7)
        sql = store._conn.execute(
            "SELECT COUNT(*) FROM crawl_run WHERE run_date='2026-10-09'"
        ).fetchone()[0]
        store.close()
        assert len(rows) == sql == 2
        by_kw = {r["keyword"]: r for r in rows}
        assert by_kw["iPhone"]["items_total"] == 90
        assert by_kw["乐高"]["result"] == "failed"
        # 汇总数 = 明细和
        assert sum(r["items_total"] for r in rows) == 100


# ---- 事件标记与手机号脱敏（T12 任务 1/4）----

class TestLogEventAndRedaction:
    def test_log_event_format(self):
        stream = io.StringIO()
        h = logging.StreamHandler(stream)
        logger = logging.getLogger("test.event")
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        log_event(logger, "keyword_done", keyword="iPhone", items=90)
        out = stream.getvalue()
        assert "EVENT[keyword_done]" in out
        assert "keyword=iPhone" in out and "items=90" in out

    def test_phone_redacted(self):
        out = redact("卖家手机号 13812345678 请联系")
        assert "13812345678" not in out
        assert "138****5678" in out
        # 非手机号数字串不受影响
        assert redact("订单号 12345678901 保留") == "订单号 12345678901 保留"

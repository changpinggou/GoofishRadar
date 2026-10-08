"""T02 单元测试：登录态判定、失效标记文件、日志脱敏、权限收紧。

全部为纯逻辑测试，不启动浏览器（浏览器流程由实况验收覆盖，见 T02-VERIFICATION.md）。
"""
from __future__ import annotations

import logging
import os

import pytest

from goofishradar.logsetup import redact
from goofishradar.session import (
    USER_COOKIE_NAMES,
    SessionManager,
    SessionState,
    evaluate_session,
    harden_permissions,
)


# ---- 登录态判定（T02 任务 2 的核心逻辑）----

class TestEvaluateSession:
    def test_valid_when_user_cookie_and_no_login_text(self):
        v = evaluate_session({"tfstk", "unb", "xlly_s"}, login_text_visible=False)
        assert v.state is SessionState.VALID

    def test_expired_when_no_user_cookie(self):
        v = evaluate_session({"tfstk", "xlly_s"}, login_text_visible=True)
        assert v.state is SessionState.EXPIRED

    def test_expired_when_user_cookie_but_login_text_visible(self):
        v = evaluate_session({"unb"}, login_text_visible=True)
        assert v.state is SessionState.EXPIRED

    def test_expired_when_no_cookie_and_no_login_text(self):
        # 页面异常（无用户 Cookie 也无登录入口）必须判失效，宁可误报不可漏报
        v = evaluate_session(set(), login_text_visible=False)
        assert v.state is SessionState.EXPIRED

    def test_risk_blocked_has_highest_priority(self):
        v = evaluate_session({"unb"}, login_text_visible=False, risk_blocked=True)
        assert v.state is SessionState.RISK_BLOCKED

    def test_user_cookie_names_are_user_signals_only(self):
        # tfstk/xlly_s 未登录即存在（T01 实测），绝不能作为登录信号
        assert "tfstk" not in USER_COOKIE_NAMES
        assert "unb" in USER_COOKIE_NAMES


# ---- 失效标记文件（T02 任务 3）----

class TestMarkerFile:
    @pytest.fixture
    def mgr(self, tmp_path):
        return SessionManager(user_data_dir=tmp_path / "ud",
                              marker_path=tmp_path / "SESSION_EXPIRED",
                              logger=logging.getLogger("test.marker"))

    def test_mark_creates_file_with_reason(self, mgr):
        mgr.mark_expired("缺少用户Cookie")
        assert mgr.marker_exists()
        content = mgr.marker_path.read_text(encoding="utf-8")
        assert "SESSION_EXPIRED" in content
        assert "缺少用户Cookie" in content

    def test_clear_removes_file(self, mgr):
        mgr.mark_expired("x")
        mgr.clear_marker()
        assert not mgr.marker_exists()
        mgr.clear_marker()  # 幂等
        assert not mgr.marker_exists()


# ---- 日志脱敏（T02 任务 4：日志不得出现 Cookie 明文）----

class TestRedact:
    @pytest.mark.parametrize("raw,secret", [
        ("unb=1234567890; sg=abc", "1234567890"),
        ("dnk=小明的闲鱼店", "小明的闲鱼店"),
        ('tracknick: "user123abc"', "user123abc"),
        ("Cookie: cna=xxxxxxxx/yyyy; unb=999", "xxxxxxxx/yyyy"),
        ("header sgcookie=AbCdEf123456 tail", "AbCdEf123456"),
        ("token _tb_token_=abcdef123456 end", "abcdef123456"),
        ("long-token 0123456789abcdef0123456789abcdef0123456789 end", None),
    ])
    def test_secret_never_survives(self, raw, secret):
        out = redact(raw)
        assert "REDACTED" in out or "<TOKEN>" in out
        if secret is not None:
            assert secret not in out

    def test_normal_log_line_untouched(self):
        line = "采集关键词 iPhone 第 2 页，共 30 条"
        assert redact(line) == line

    def test_filter_masks_record(self, caplog):
        logger = logging.getLogger("test.redact")
        handler = logging.StreamHandler()
        handler.addFilter.__self__  # noqa: B018 - 确认 Filter 可挂载
        from goofishradar.logsetup import RedactFilter
        handler.addFilter(RedactFilter())
        logger.addHandler(handler)
        logger.propagate = True
        with caplog.at_level(logging.INFO):
            logger.info("cookies: unb=987654321 done")
        assert "987654321" not in caplog.text
        assert "<REDACTED>" in caplog.text


# ---- 权限收紧（T02 任务 4：会话文件 600，目录 700）----

class TestHardenPermissions:
    def test_dirs_700_files_600(self, tmp_path):
        target = tmp_path / "user_data"
        (target / "Default" / "sub").mkdir(parents=True)
        f1 = target / "Cookies"
        f1.write_text("x")
        f2 = target / "Default" / "Preferences"
        f2.write_text("y")
        link = target / "link"
        os.symlink(f1, link)

        os.chmod(target, 0o755)
        harden_permissions(target)

        assert stat_mode(target) == 0o700
        assert stat_mode(target / "Default") == 0o700
        assert stat_mode(target / "Default" / "sub") == 0o700
        assert stat_mode(f1) == 0o600
        assert stat_mode(f2) == 0o600

    def test_missing_path_is_noop(self, tmp_path):
        harden_permissions(tmp_path / "nope")  # 不抛异常即通过


def stat_mode(path) -> int:
    return os.stat(path).st_mode & 0o777

"""会话管理模块（T02）：扫码登录、会话校验、失效标记与告警、权限收紧。

T01 实测约束（详见 docs/字段核对清单.md）：
- headless 访问数据页会被风控拦截 —— 扫码登录强制 headed；check 默认 headless 仅访问
  首页壳（已验证安全），若首页也出现「非法访问」则返回 RISK_BLOCKED 提示用 --headed；
- 登录态判定信号 = 用户 Cookie（unb/dnk/tracknick/lgc 之一）存在 且 页面无登录入口；
- 任何日志不得输出 Cookie 值（logsetup.redact 兜底，本模块只记录 Cookie 名）。
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from playwright.sync_api import sync_playwright

from .logsetup import get_logger
from .paths import HOME_URL, MARKER_EXPIRED, USER_DATA_DIR

USER_COOKIE_NAMES = frozenset({"unb", "dnk", "tracknick", "lgc"})
LOGIN_TEXTS = ("亲，请登录", "请登录", "登录")
# 阿里系风控/验证页特征（T02 实测：headless 或高频后首页也可能被拦，且呈间歇性）
RISK_MARKERS = ("非法访问", "请使用正常浏览器", "安全验证", "访问受限", "滑块验证", "验证码")

VIEWPORT = {"width": 1440, "height": 900}


class SessionState(Enum):
    VALID = "valid"
    EXPIRED = "expired"
    RISK_BLOCKED = "risk_blocked"


@dataclass
class SessionVerdict:
    state: SessionState
    reason: str

    @property
    def valid(self) -> bool:
        return self.state is SessionState.VALID


class SessionExpiredError(RuntimeError):
    """会话失效。采集任务（T04/T08）应捕获后终止本次任务并以退出码 1 结束。"""


def detect_risk_block(text: str) -> bool:
    """页面文本是否命中风控/验证页特征（session 与 collector 共用）。"""
    return any(m in (text or "") for m in RISK_MARKERS)


def evaluate_session(
    cookie_names: Iterable[str], login_text_visible: bool, risk_blocked: bool = False,
) -> SessionVerdict:
    """登录态判定（纯函数，可单测）：Cookie 信号为主，页面登录入口为辅。"""
    if risk_blocked:
        return SessionVerdict(SessionState.RISK_BLOCKED, "页面触发风控拦截（非法访问页）")
    has_user = bool(set(cookie_names) & USER_COOKIE_NAMES)
    if has_user and not login_text_visible:
        return SessionVerdict(SessionState.VALID, "用户Cookie存在且页面无登录入口")
    if not has_user:
        return SessionVerdict(SessionState.EXPIRED,
                              f"缺少用户Cookie {sorted(USER_COOKIE_NAMES)}")
    return SessionVerdict(SessionState.EXPIRED, "页面出现登录入口，登录态已失效")


def harden_permissions(path: Path) -> None:
    """收紧会话目录权限（T02 任务 4）：目录 700、文件 600、软链不动。"""
    path = Path(path)
    if not path.exists():
        return
    os.chmod(path, 0o700)
    for root, dirs, files in os.walk(path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), 0o700)
            except OSError:
                pass
        for f in files:
            fp = os.path.join(root, f)
            if os.path.islink(fp):
                continue
            try:
                os.chmod(fp, 0o600)
            except OSError:
                pass


def _login_text_visible(page) -> bool:
    for text in LOGIN_TEXTS:
        try:
            if page.get_by_text(text, exact=False).count() > 0:
                return True
        except Exception:
            continue
    return False


def _click_login(page) -> bool:
    """点击页头「登录」入口：先按文本候选逐个试可见元素，最后用 JS 精确匹配兜底。

    T02 实测：get_by_text(...).first 常命中不可见元素导致漏点，故遍历候选 + JS 兜底。
    """
    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=5000)
                    return True
        except Exception:
            continue
    try:
        return page.evaluate("""() => {
          const els = [...document.querySelectorAll('a, button, span, div')];
          const el = els.find(e =>
            e.textContent && e.textContent.trim() === '登录' && e.offsetParent !== null);
          if (el) { el.click(); return true; }
          return false;
        }""")
    except Exception:
        return False


class SessionManager:
    def __init__(self, user_data_dir: Path = USER_DATA_DIR,
                 marker_path: Path = MARKER_EXPIRED, logger=None,
                 wait_seconds: int = 300):
        self.user_data_dir = Path(user_data_dir)
        self.marker_path = Path(marker_path)
        self.wait_seconds = wait_seconds
        self.log = logger or get_logger()

    # ---- 失效标记文件（T02 任务 3）----

    def marker_exists(self) -> bool:
        return self.marker_path.exists()

    def mark_expired(self, reason: str) -> None:
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.marker_path.write_text(f"{stamp} SESSION_EXPIRED: {reason}\n", encoding="utf-8")
        self.log.warning("ALERT: 登录态失效，已置标记 %s —— %s", self.marker_path.name, reason)

    def clear_marker(self) -> None:
        if self.marker_path.exists():
            self.marker_path.unlink()
            self.log.info("登录态恢复，已清除失效标记 %s", self.marker_path.name)

    # ---- 会话校验（T02 任务 2）----

    def check(self, headless: bool = True) -> SessionVerdict:
        """轻量校验：headless 访问首页壳，凭 Cookie/登录入口判定（不做搜索，规避风控）。"""
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(self.user_data_dir), headless=headless, locale="zh-CN",
                viewport=VIEWPORT)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(5000)
                verdict = self._verdict_from(page, ctx)
                self._debug_screenshot(page, verdict)
            finally:
                ctx.close()
        self._after_verdict(verdict)
        return verdict

    def ensure_valid(self) -> None:
        """供采集流程调用：失效则抛 SessionExpiredError（终止任务）。"""
        verdict = self.check(headless=True)
        if not verdict.valid:
            raise SessionExpiredError(verdict.reason)

    # ---- 扫码登录（T02 任务 1，强制 headed）----

    def login(self, wait_seconds: int | None = None) -> SessionVerdict:
        wait_s = wait_seconds or self.wait_seconds
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(self.user_data_dir), headless=False, locale="zh-CN",
                viewport=VIEWPORT)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(5000)
                verdict = self._verdict_from(page, ctx)
                self._debug_screenshot(page, verdict)
                if verdict.valid:
                    self.log.info("已有有效登录态，免扫码")
                    ctx.close()
                    self._after_verdict(verdict)
                    return verdict

                if verdict.state is SessionState.RISK_BLOCKED:
                    self.log.warning("登录页触发风控拦截，请稍后重试或更换网络环境")
                    ctx.close()
                    self._after_verdict(verdict)
                    return verdict

                if not _click_login(page):
                    self.log.info("未自动定位登录入口，请在窗口中手动点击「登录」")
                self.log.info("请在浏览器窗口中用闲鱼/淘宝 App 扫码登录（最长等待 %d 秒）...", wait_s)

                deadline = time.monotonic() + wait_s
                while time.monotonic() < deadline:
                    if self._has_user_cookie(ctx):
                        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
                        page.wait_for_timeout(4000)
                        verdict = self._verdict_from(page, ctx)
                        self._debug_screenshot(page, verdict)
                        if verdict.valid:
                            self.log.info("扫码登录成功，会话已持久化")
                            break
                    time.sleep(3)
                else:
                    verdict = SessionVerdict(SessionState.EXPIRED,
                                             f"扫码登录超时（等待 {wait_s} 秒）")
            finally:
                ctx.close()
        self._after_verdict(verdict)
        return verdict

    # ---- 内部 ----

    def _verdict_from(self, page, ctx) -> SessionVerdict:
        try:
            body = page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 3000) : ''")
        except Exception:
            body = ""
        risk_blocked = detect_risk_block(body)
        cookie_names = {c["name"] for c in ctx.cookies()}
        return evaluate_session(cookie_names, _login_text_visible(page), risk_blocked)

    def _debug_screenshot(self, page, verdict: SessionVerdict) -> None:
        """非有效判定时留存诊断截图（本地 data/samples/screens，gitignored）。"""
        try:
            if verdict.state is SessionState.VALID:
                return
            shots = self.user_data_dir.parent / "samples" / "screens"
            shots.mkdir(parents=True, exist_ok=True)
            name = f"session_{verdict.state.value}_{time.strftime('%Y%m%d_%H%M%S')}.png"
            page.screenshot(path=str(shots / name))
            self.log.debug("诊断截图: %s", name)
        except Exception:
            pass

    @staticmethod
    def _has_user_cookie(ctx) -> bool:
        names = {c["name"] for c in ctx.cookies()}
        return bool(names & USER_COOKIE_NAMES)

    def _after_verdict(self, verdict: SessionVerdict) -> None:
        if verdict.valid:
            self.clear_marker()
        else:
            self.mark_expired(verdict.reason)
        harden_permissions(self.user_data_dir)

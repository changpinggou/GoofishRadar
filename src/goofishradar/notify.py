"""告警推送（T12 任务 3 / FR-9 P1）：Notify 抽象 + Webhook（钉钉/飞书）+ SMTP 邮件。

- 渠道经 settings.notify 配置；未配置 → 降级为本地标记（data/ALERT 追加）+ 日志；
- 推送失败不抛出（不拖垮采集任务）：记录错误并降级本地标记；
- 使用标准库 urllib/smtplib，不引入新依赖。
"""
from __future__ import annotations

import json
import smtplib
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

from .config import Settings
from .logsetup import get_logger


@dataclass
class Alert:
    level: str          # warning / critical
    event: str          # session_expired / consecutive_failures / low_volume /
                        # high_parse_failure / risk_blocked
    message: str
    date: str

    def render(self) -> str:
        return f"[GoofishRadar·{self.level}] {self.event}（{self.date}）：{self.message}"


class WebhookNotifier:
    """钉钉/飞书群机器人文本消息。"""

    name = "webhook"

    def __init__(self, url: str, channel: str = "feishu", timeout: int = 10):
        self.url, self.channel, self.timeout = url, channel, timeout

    def payload(self, alert: Alert) -> dict:
        text = alert.render()
        if self.channel == "dingtalk":
            return {"msgtype": "text", "text": {"content": text}}
        return {"msg_type": "text", "content": {"text": text}}   # 飞书（默认）

    def send(self, alert: Alert) -> None:
        req = urllib.request.Request(
            self.url, data=json.dumps(self.payload(alert)).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"webhook 返回 {resp.status}")


class SmtpNotifier:
    """SMTP 邮件（密码建议经环境变量 GOOFISH_SMTP_PASSWORD 注入）。"""

    name = "smtp"

    def __init__(self, host: str, port: int, user: str, password: str,
                 to: str, use_tls: bool = True, timeout: int = 15):
        self.host, self.port = host, int(port)
        self.user, self.password, self.to = user, password, to
        self.use_tls, self.timeout = use_tls, timeout

    def send(self, alert: Alert) -> None:
        msg = MIMEText(alert.render(), "plain", "utf-8")
        msg["Subject"] = f"GoofishRadar 告警：{alert.event}"
        msg["From"] = self.user
        msg["To"] = self.to
        if self.use_tls and self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout) as s:
                s.login(self.user, self.password)
                s.sendmail(self.user, [self.to], msg.as_string())
        else:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
                if self.use_tls:
                    s.starttls()
                s.login(self.user, self.password)
                s.sendmail(self.user, [self.to], msg.as_string())


def build_notifiers(settings: Settings) -> list:
    """按配置实例化渠道；未配置返回空列表（调用方降级本地标记）。"""
    n = settings.notify
    out = []
    if n.webhook_url:
        out.append(WebhookNotifier(n.webhook_url, n.webhook_channel))
    if n.smtp_host:
        import os
        password = n.smtp_password or os.environ.get("GOOFISH_SMTP_PASSWORD", "")
        out.append(SmtpNotifier(n.smtp_host, n.smtp_port, n.smtp_user,
                                password, n.smtp_to, n.smtp_use_tls))
    return out


def _append_local_marker(data_dir: Path, alert: Alert) -> None:
    marker = data_dir / "ALERT"
    with marker.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} "
                f"{alert.level} {alert.event}: {alert.message}\n")


def send_alerts(alerts: list[Alert], settings: Settings, log=None) -> dict:
    """批量发送告警。未配置渠道/发送失败均降级本地标记（data/ALERT 追加）。"""
    log = log or get_logger()
    if not alerts:
        return {"sent": {}, "degraded": 0}
    notifiers = build_notifiers(settings)
    result: dict[str, str] = {}
    degraded = 0
    for alert in alerts:
        if not notifiers:
            degraded += 1
            _append_local_marker(Path(settings.paths.data_dir), alert)
            continue
        for notifier in notifiers:
            try:
                notifier.send(alert)
                result[f"{notifier.name}:{alert.event}"] = "ok"
            except Exception as exc:
                degraded += 1
                result[f"{notifier.name}:{alert.event}"] = f"failed: {exc}"
                log.error("告警推送失败（%s/%s）：%s —— 已降级本地标记",
                          notifier.name, alert.event, exc)
                _append_local_marker(Path(settings.paths.data_dir), alert)
    if notifiers and degraded == 0:
        for alert in alerts:
            log.info("告警已推送：%s", alert.render())
    return {"sent": result, "degraded": degraded}

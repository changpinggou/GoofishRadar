"""日志配置：控制台 + 按日滚动文件，带 Cookie 脱敏过滤器（T02 任务 4）。

红线：任何日志不得出现 Cookie 值明文。本模块提供 redact() 纯函数（可单测），
并以 RedactFilter 挂载到所有 handler 作最后防线。
"""
from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler

from .paths import LOG_DIR

# 已知敏感 Cookie 名的赋值/键值形态（值可含中文昵称等非 ASCII），以及 Cookie 请求头整体
_RE_COOKIE_KV = re.compile(
    r"\b(unb|dnk|tracknick|lgc|sgcookie|cookie2|_tb_token_|tfstk|xlly_s|cna|isg)"
    r"\s*[=:]\s*[\"']?[^\s;,\"']{4,}",
    re.IGNORECASE,
)
_RE_COOKIE_HEADER = re.compile(r"\bCookie\s*:\s*\S+", re.IGNORECASE)
_RE_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_/+=]{40,}\b")  # 兜底：40+ 位疑似令牌串

def redact(text: str) -> str:
    """脱敏日志文本：Cookie 键值对 / Cookie 头 / 超长令牌串一律打码。"""
    text = _RE_COOKIE_KV.sub(lambda m: f"{m.group(1)}=<REDACTED>", text)
    text = _RE_COOKIE_HEADER.sub("Cookie: <REDACTED>", text)
    text = _RE_LONG_TOKEN.sub("<TOKEN>", text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def get_logger(name: str = "goofishradar") -> logging.Logger:
    """配置一次并返回项目 logger：控制台 INFO + 文件 DEBUG（按日滚动，保留 30 天）。"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    console.addFilter(RedactFilter())
    logger.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        LOG_DIR / "goofishradar.log", when="midnight", backupCount=30, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    file_handler.addFilter(RedactFilter())
    logger.addHandler(file_handler)
    return logger

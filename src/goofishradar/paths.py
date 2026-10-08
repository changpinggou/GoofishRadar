"""项目路径常量与目录初始化。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
USER_DATA_DIR = DATA_DIR / "user_data"      # Playwright 持久化会话目录（敏感，gitignored）
LOG_DIR = ROOT / "logs"

MARKER_EXPIRED = DATA_DIR / "SESSION_EXPIRED"  # 会话失效标记文件（T02 验收要求）

HOME_URL = "https://www.goofish.com/"

for _d in (DATA_DIR, USER_DATA_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

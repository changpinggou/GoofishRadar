"""T01 PoC 公共工具：路径常量、字段定义、样本落盘。

样本统一归档到 data/samples/（gitignored，仅本地留存 —— 合规要求，见需求文档 R4）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
SAMPLES = DATA / "samples"
RAW = SAMPLES / "raw"
SCREENS = SAMPLES / "screens"
USER_DATA = DATA / "user_data"

for _d in (DATA, SAMPLES, RAW, SCREENS, USER_DATA):
    _d.mkdir(parents=True, exist_ok=True)

HOME_URL = "https://www.goofish.com/"
SEARCH_URL = "https://www.goofish.com/search?q={kw}"

# 《字段核对清单》覆盖的核对字段（对应需求文档第 9 节数据字典）
FIELDS = [
    "item_id", "title", "price", "want_count", "view_count", "publish_time",
    "status", "seller_nick", "seller_id", "location", "pic_url", "item_url",
]
FIELD_LABELS = {
    "item_id": "商品唯一标识",
    "title": "商品标题",
    "price": "当前售价",
    "want_count": "想要数",
    "view_count": "浏览量",
    "publish_time": "发布时间",
    "status": "商品状态(在售/已售)",
    "seller_nick": "卖家昵称",
    "seller_id": "卖家标识",
    "location": "所在地",
    "pic_url": "商品图片URL",
    "item_url": "商品链接",
}


def ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def save_sample(name: str, content: "bytes | str") -> Path:
    path = RAW / name
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    log(f"样本已归档: {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return path


def save_result(name: str, obj) -> Path:
    path = SAMPLES / name
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"结果已写入: {path.relative_to(ROOT)}")
    return path


def fail(msg: str, code: int) -> "NoReturn":  # type: ignore[name-defined]
    log(f"FAIL: {msg}")
    sys.exit(code)

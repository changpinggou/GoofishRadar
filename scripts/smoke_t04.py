"""T04 冒烟脚本：按配置真实采集若干关键词，输出汇总。

用法：
  .venv/bin/python scripts/smoke_t04.py                       # 全部启用任务
  .venv/bin/python scripts/smoke_t04.py --keywords iPhone     # 指定关键词
  .venv/bin/python scripts/smoke_t04.py --pages 1             # 覆盖页数（探测用）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goofishradar.config import enabled_tasks, load_keywords, load_settings  # noqa: E402
from goofishradar.collector import SearchCollector  # noqa: E402
from goofishradar.logsetup import get_logger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keywords", default="", help="逗号分隔，缺省=全部启用任务")
    parser.add_argument("--pages", type=int, default=0, help="覆盖每个任务的页数")
    args = parser.parse_args()

    log = get_logger()
    settings, s_warn = load_settings()
    tasks, k_warn = load_keywords()
    for w in s_warn + k_warn:
        log.warning("配置：%s", w)
    settings.paths.ensure()

    active = enabled_tasks(tasks)
    if args.keywords:
        wanted = [k.strip() for k in args.keywords.split(",") if k.strip()]
        active = [t for t in active if t.keyword in wanted]
    if args.pages:
        for t in active:
            t.max_pages = args.pages
    if not active:
        log.error("没有可运行的采集任务")
        return 1

    collector = SearchCollector(settings)
    results, aborted = collector.collect_keywords(active)

    day = datetime.now().strftime("%Y-%m-%d")
    stamp = datetime.now().strftime("%H%M%S")
    summary = {
        "day": day, "aborted_by_risk": aborted,
        "keywords": [{
            "keyword": r.keyword, "error": r.error,
            "pages_attempted": r.pages_attempted, "pages_ok": r.pages_ok,
            "degraded_pages": r.degraded_pages,
            "parse_success_rate": round(r.parse_success_rate, 3),
            "items": len(r.items),
            "missing_field_count": r.missing_field_count,
        } for r in results],
        "total_items": sum(len(r.items) for r in results),
        "total_pages_attempted": sum(r.pages_attempted for r in results),
        "total_pages_ok": sum(r.pages_ok for r in results),
    }
    out = Path(settings.paths.data_dir) / "parsed" / day / f"_smoke_{stamp}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    log.info("汇总已写入 %s", out)
    return 1 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())

"""GoofishRadar 命令入口（T02：login / check；T08 将扩展 run / status / report）。

退出码约定：
- login：0 成功（含免扫码复用）；10 扫码超时/失效；4 风控拦截
- check：0 有效；1 失效（采集任务应终止）；4 风控拦截
"""
from __future__ import annotations

import argparse
import sys

from .logsetup import get_logger
from .session import SessionState, SessionManager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="goofishradar", description="GoofishRadar — 闲鱼畅销榜自动化工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="扫码登录并持久化会话（必须有头，弹出浏览器窗口）")
    p_login.add_argument("--wait", type=int, default=300, help="等待扫码秒数（默认 300）")

    p_check = sub.add_parser("check", help="校验会话有效性（默认有头轻量访问首页）")
    p_check.add_argument("--headless", action="store_true",
                         help="无头模式校验（T02 实测 headless 可能被风控拦截首页，故默认有头）")

    args = parser.parse_args(argv)
    log = get_logger()
    mgr = SessionManager()

    if args.command == "login":
        verdict = mgr.login(wait_seconds=args.wait)
        if verdict.valid:
            return 0
        log.error("登录失败：%s", verdict.reason)
        return 4 if verdict.state is SessionState.RISK_BLOCKED else 10

    if args.command == "check":
        verdict = mgr.check(headless=not args.headless)
        if verdict.valid:
            log.info("会话有效（%s）", verdict.reason)
            return 0
        if verdict.state is SessionState.RISK_BLOCKED:
            log.error("风控拦截：%s —— 请稍后重试或更换网络环境", verdict.reason)
            return 4
        log.error("会话失效：%s —— 请运行 goofishradar login 重新扫码", verdict.reason)
        return 1

    return 2  # 不可达（argparse required=True）


if __name__ == "__main__":
    sys.exit(main())

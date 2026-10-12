# GoofishRadar — 闲鱼畅销商品排行榜自动化工具

> GoofishRadar：一台每天对闲鱼（Goofish）全域做"雷达扫描"的自动化工具——
> 定时采集在售商品热度，自动生成畅销榜总榜、分类榜与热度飙升榜。

每日定时采集闲鱼（www.goofish.com）在售商品数据，基于「想要数 / 浏览量 / 日增量」计算畅销度评分，
自动生成总榜、分类榜与热度飙升榜。

- 需求文档：[闲鱼畅销榜自动化工具-需求文档.md](./闲鱼畅销榜自动化工具-需求文档.md)
- 需求单：[闲鱼畅销榜自动化工具-需求单.md](./闲鱼畅销榜自动化工具-需求单.md)
- **使用手册：[USER_GUIDE.md](./USER_GUIDE.md)（三步上手）**
- UAT 验收报告：[UAT-REPORT.md](./UAT-REPORT.md)（含观察期计划）
- **迁移指引：[迁移指引.md](./迁移指引.md)（更换电脑时的资产清单与步骤）**
- 验收记录：`T01`–`T13`-VERIFICATION.md（每完成一张需求单一份）

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖（需 Python ≥ 3.11 与 uv）
uv venv && uv pip install -p .venv/bin/python -e "."
.venv/bin/playwright install chromium

# 2. 会话管理（T02）：扫码登录一次并持久化（弹出浏览器窗口，用闲鱼/淘宝 App 扫码）
.venv/bin/goofishradar login

#    校验会话有效性：有效 exit 0；失效 exit 1（写告警日志 + data/SESSION_EXPIRED 标记）
.venv/bin/goofishradar check

# 3. 每日任务（T08）：会话检查→采集→入库→评分→报告，一条命令
.venv/bin/goofishradar run

#    近 7 天运行概况 / 基于库内数据重出报告
.venv/bin/goofishradar status
.venv/bin/goofishradar report --date 2026-10-08

# 4. 每日定时调度（T09）：每天 08:00±15 分钟自动运行（工作日/周末时刻可在配置区分）
.venv/bin/goofishradar schedule install      # 安装/刷新（幂等）
.venv/bin/goofishradar schedule status       # 查看安装状态与日志路径
.venv/bin/goofishradar schedule uninstall    # 移除
#    断点续采：当日已成功关键词重跑时自动跳过；run --force 强制重采

# 3. PoC（T01）：免登录首页探测
.venv/bin/python scripts/poc/poc1_homepage_probe.py

# 4. PoC（T01）：搜索字段核对（会弹出浏览器窗口）
.venv/bin/python scripts/poc/poc2_login_search_probe.py --keyword "iPhone"

# 5. 分析字段核对结果
.venv/bin/python scripts/poc/poc3_analyze.py

# 6. 真实采集冒烟（T04，弹出浏览器窗口，登录态必须有效）
.venv/bin/python scripts/smoke_t04.py --keywords iPhone --pages 1
```

> 合规约束（见需求文档 FR-10 / R1 / R4）：仅采集登录后公开可见页面数据；全局限速、单日页数硬上限；
> 不绕过验证码/登录墙、不逆向加密接口；数据与样本仅本地自用，不入 git、不对外分发。

## 合规自查清单（T11）

- [x] **不绕过验证码/登录墙**：检测到验证码/安全拦截页立即中止全部采集并进入冷却（默认 120 分钟），不做任何识别、打码或环境伪装；
- [x] **不逆向加密接口**：商品数据来自页面自身 mtop 响应的被动记录（Playwright 监听），不构造/伪造任何签名请求；
- [x] **仅公开数据**：只采集登录后公开可见的搜索结果列表页，不访问任何需额外权限的内容；
- [x] **仅个人自用**：原始样本、会话、数据库均本地留存且 gitignored，不对外分发（报告含固定提示）；
- [x] **全局限速硬约束**：任意相邻页面请求间隔 ≥ 配置区间（GlobalThrottle 强制，跨关键词生效）；单日页数硬上限 300（代码层截断，配置不可突破）；请求逐条审计（`logs/request_audit_*.jsonl` 可复核间隔）；
- [x] **错峰启动**：每日调度在配置时刻 ±15 分钟内随机执行（工作日/周末可区分）；
- [x] **仅 headed 真实浏览器**：采集全程有头 Chromium + 真实登录会话；
- [x] **日志脱敏**：Cookie 键值/请求头/长令牌一律打码（RedactFilter），实测无 Cookie 明文。

## T01 PoC 技术结论（2026-10-08，详见 `docs/字段核对清单.md` 与 `T01-VERIFICATION.md`）

1. **登录态**：扫码登录一次后，持久化 `user_data_dir` 跨进程复用会话**验证通过**（重启免扫码）；
2. **数据路径**：页面为 ICE SPA，商品数据在 mtop JSON 接口中（搜索主接口 `mtop.taobao.idlemtopsearch.pc.search`），
   通过页面响应监听**被动记录**即可获得，无需逆向签名；
3. **字段**：列表页直接可得 10/12（标题/价格/想要数/卖家/所在地/图片/链接等），
   浏览量与发布时间由**详情页**补齐（`browseCnt`/`GMT_CREATE_DATE_KEY`），支撑 FR-4 设计；
4. **风控红线**：headless 访问数据页会被拦截（「非法访问」页），**采集必须 headed 模式**，不做环境伪装；
5. **翻页**：页码分页控件（非无限滚动），点页码加载约 7s/页；单页约 30 条；
6. **限速基线**：单页稳定耗时 6–9s，建议页间延时 8–20s（待 T11 定稿）。


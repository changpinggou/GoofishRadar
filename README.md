# GoofishRadar — 闲鱼畅销商品排行榜自动化工具

> GoofishRadar：一台每天对闲鱼（Goofish）全域做"雷达扫描"的自动化工具——
> 定时采集在售商品热度，自动生成畅销榜总榜、分类榜与热度飙升榜。

每日定时采集闲鱼（www.goofish.com）在售商品数据，基于「想要数 / 浏览量 / 日增量」计算畅销度评分，
自动生成总榜、分类榜与热度飙升榜。

- 需求文档：[闲鱼畅销榜自动化工具-需求文档.md](./闲鱼畅销榜自动化工具-需求文档.md)
- 需求单：[闲鱼畅销榜自动化工具-需求单.md](./闲鱼畅销榜自动化工具-需求单.md)
- 验收记录：`T01-VERIFICATION.md`（每完成一张需求单新增一份）

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖（需 Python ≥ 3.11 与 uv）
uv venv && uv pip install -p .venv/bin/python -e "."
.venv/bin/playwright install chromium

# 2. PoC（T01）：免登录首页探测
.venv/bin/python scripts/poc/poc1_homepage_probe.py

# 3. PoC（T01）：扫码登录 + 搜索字段核对（会弹出浏览器窗口，请用闲鱼/淘宝 App 扫码）
.venv/bin/python scripts/poc/poc2_login_search_probe.py --keyword "iPhone"

# 4. 分析字段核对结果
.venv/bin/python scripts/poc/poc3_analyze.py
```

> 合规约束（见需求文档 FR-10 / R1 / R4）：仅采集登录后公开可见页面数据；全局限速、单日页数硬上限；
> 不绕过验证码/登录墙、不逆向加密接口；数据与样本仅本地自用，不入 git、不对外分发。

## T01 PoC 技术结论（2026-10-08，详见 `docs/字段核对清单.md` 与 `T01-VERIFICATION.md`）

1. **登录态**：扫码登录一次后，持久化 `user_data_dir` 跨进程复用会话**验证通过**（重启免扫码）；
2. **数据路径**：页面为 ICE SPA，商品数据在 mtop JSON 接口中（搜索主接口 `mtop.taobao.idlemtopsearch.pc.search`），
   通过页面响应监听**被动记录**即可获得，无需逆向签名；
3. **字段**：列表页直接可得 10/12（标题/价格/想要数/卖家/所在地/图片/链接等），
   浏览量与发布时间由**详情页**补齐（`browseCnt`/`GMT_CREATE_DATE_KEY`），支撑 FR-4 设计；
4. **风控红线**：headless 访问数据页会被拦截（「非法访问」页），**采集必须 headed 模式**，不做环境伪装；
5. **翻页**：页码分页控件（非无限滚动），点页码加载约 7s/页；单页约 30 条；
6. **限速基线**：单页稳定耗时 6–9s，建议页间延时 8–20s（待 T11 定稿）。


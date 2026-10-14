# GoofishRadar 使用手册

> 三步上手：**扫码登录 → 配置关键词 → 出报告**。全程约 10 分钟。

## 一、安装（一次性）

要求：macOS、Python ≥ 3.11、[uv](https://docs.astral.sh/uv/)。

```bash
cd /Users/zego/Documents/LocalGoofish
uv venv && uv pip install -p .venv/bin/python -e "."
.venv/bin/playwright install chromium
```

## 二、三步上手

### 第 1 步：扫码登录（会话长期复用）

```bash
.venv/bin/goofishradar login
```

会弹出浏览器窗口，用**闲鱼/淘宝 App** 扫码。成功后会话持久化在 `data/user_data/`，
之后所有命令免扫码。会话失效时 `run`/`check` 会告警并提示重新扫码。

### 第 2 步：配置关键词

编辑 `config/keywords.yaml`（或用命令）：

```yaml
tasks:
  - keyword: "iPhone"      # 关键词
    enabled: true          # 启用开关
    max_pages: 3           # 采集页数（硬顶 50）
    sort: "most_want"      # most_want=平台默认综合序（平台无「最多想要」排序项）
    price_min: 500         # 可选：价格/成色(stuff)/所在地(location) 筛选
    price_max: 8000
```

```bash
.venv/bin/goofishradar keyword list            # 查看
.venv/bin/goofishradar keyword add 乐高 --pages 2
.venv/bin/goofishradar keyword remove 乐高
.venv/bin/goofishradar config-check            # 校验配置并打印生效值
```

全局参数（页数上限、TopN、评分权重等）在 `config/settings.yaml`，同样改文件即生效。

### 第 3 步：运行与查看报告

```bash
.venv/bin/goofishradar run          # 采集→入库→评分→报告，一条命令（约 10 分钟）
open reports/$(date +%F)/report.html
```

报告内容：**总榜 Top50 / 每关键词 Top20 / 热度飙升榜**，含价格、想要数±日增量、
评分、卖家、所在地、状态，可点击标题跳转闲鱼；同目录 `rankings.xlsx` 为 4 Sheet Excel。

## 三、运行方式（仅手动运行）

本工具**不会每日自动运行**——定时调度功能已移除，也没有任何开机自启动，
只有你手动执行 `run` 时才会采集：

```bash
.venv/bin/goofishradar run          # 手动触发一次完整采集
```

- 建议每天固定时间手动跑一次：想要数/浏览量的日增量与飙升榜依赖每日快照的连续性；
- **断点续采**：当日已成功关键词重跑时自动跳过（`run --force` 强制重采）。

## 四、日常运维命令

| 命令 | 用途 |
| --- | --- |
| `goofishradar status` | 近 7 天运行概况（按日汇总、逐词明细、报告✓/✗、告警标记） |
| `goofishradar check` | 校验会话是否有效（exit 0 有效 / 1 失效 / 4 风控冷却中） |
| `goofishradar report --date 2026-10-10` | 基于库内数据重出某日报告 |
| `goofishradar item-history <商品ID>` | 商品逐日想要数/浏览量趋势（CSV） |
| `goofishradar snapshot-archive` | 归档超 90 天的明细快照（导出 CSV 后清理） |
| `goofishradar run --keyword iPhone` | 只跑指定关键词 |

## 五、告警推送（可选）

编辑 `config/settings.yaml` 的 `notify:` 节（留空=禁用，降级为本地标记 `data/ALERT`）：

```yaml
notify:
  webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"   # 飞书机器人
  webhook_channel: "feishu"       # feishu | dingtalk
  smtp:                            # 或邮件渠道
    host: "smtp.qq.com"
    port: 465
    user: "you@qq.com"
    password: ""                   # 建议留空，用环境变量 GOOFISH_SMTP_PASSWORD 注入
    to: "you@qq.com"
```

告警场景：会话失效、连续两天全败、日采集 <100 条、解析失败率 >20%、风控中止。

## 六、重要口径说明

1. **畅销度 = 热度代理指标**：闲鱼不公开销量，评分为「想要数/浏览量及其增量」的加权归一化值，
   **不是实际成交量**（报告顶部固定标注）；
2. **权重自动归并**：列表页无浏览量 → 浏览类权重暂并入想要类；新商品为主的任务无历史 →
   增量权重并入绝对值。逐任务有效权重见报告 `weights_applied`；
3. **飙升榜**需要 ≥2 天历史快照；商品停止采集后增量冻结并在趋势查询中标注"数据中断"；
4. 飙升榜日增量在采集中断日之后，按最近一次可用快照为基线（可能是多日累计）。

## 七、常见问题

| 现象 | 处理 |
| --- | --- |
| `run` 提示会话失效 | `goofishradar login` 重新扫码 |
| `check` 提示风控冷却中（exit 4） | 等待冷却结束（标记 `data/RISK_COOLDOWN_UNTIL`），勿反复重试 |
| 页面"非法访问"拦截 | 风控触发：工具已自动进入冷却；降低采集页数/频率，通常数小时至隔日恢复 |
| 报告显示"部分完成" | 单日预算用尽或风控中止，当日数据不完整属预期标注 |
| 修改配置不生效 | 启动时报错会指明字段；用 `config-check` 核对生效值 |
| 想重采当日数据 | `run --force`（跳过断点续采） |

## 八、合规边界（务必遵守）

仅采集登录后公开可见数据；不绕过验证码/登录墙；不逆向加密接口；仅 headed 真实浏览器；
全局限速+单日 300 页硬上限+错峰启动（代码强制，配置不可突破）；数据仅本地自用、
不对外分发。详见 README「合规自查清单」。

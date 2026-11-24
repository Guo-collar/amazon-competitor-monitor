# 亚马逊竞品监控

监控亚马逊美国站两款宠物饮水机，唯一数据源为卖家精灵 MCP V2。保存同口径基线，在每轮巡检中报告变化，达到阈值时预警。

| 竞品 | ASIN | 商品 |
|---|---|---|
| A | B0C8TG35BF | DownyPaws 无线饮水机，84 oz / 2.5 L |
| B | B0DJVX1BX6 | NautyPaws 无线不锈钢饮水机，108 oz / 3.2 L |

## 取数和对比口径

- 商品详情 `asin.detail`：美元售价、Pet Supplies 大类 BSR、平均星级、评分数 `reviewCount`、接口更新时间。
- 趋势 `keepa.trend`：取同一 ASIN 及小类节点的近期最新有效小类 BSR；报告标注其独立采样时间。
- 已验证的 V2 接口未返回库存状态，报告为未知，不用价格、购物车、卖家数量推断，也不触发售罄或回货预警。V2 快照不是实时抓取结果。
- 仅比较来源、站点、ASIN、指标口径一致且时间更新的数据。首次有效数据建立基线；同一数据点不重复预警；缺失不覆盖有效基线，不用旧值填本轮报告。币种或类目变化不跨口径比较。
- 预警：价格变化至少 $0.50 或 3%；大类 BSR 至少 20%；小类 BSR 至少 15% 或 3 位；星级至少 0.1。评分数增长至少 20% 提醒；单次暴涨超过 150% 标为口径待核查，不视作真实新增评价。价格及 BSR 阈值可在 `config.json` 调整。

## 本地运行

本地优先使用环境变量 `SELLERSPRITE_MCP_SECRET_KEY`，没有时复用 WorkBuddy 已配置的 V2 连接。不打印密钥，也不将密钥写入项目文件。

```bash
python monitor.py --dry-run  # 真实取数并预览，不写文件、不推送
python monitor.py --init     # 保存卖家精灵基线，不推送
python monitor.py            # 巡检、记录并按既有配置推送
```

- `data/sellersprite_history.json`：卖家精灵独立基线；旧 `data/history.json` 原样保留，不参与对比。
- `data/sellersprite_last_run.json`：云端定时门控。
- `data/monitor.log` 和 `data/alerts.jsonl`：当前运行环境的日志和预警记录；云端执行日志见 Actions 页面。

## GitHub Actions 部署

1. 在仓库 `Settings → Secrets and variables → Actions` 添加 `SELLERSPRITE_MCP_SECRET_KEY`。云端不读取本机配置。飞书通知沿用 `FEISHU_WEBHOOK` 及可选 `FEISHU_SECRET`；原有其他通知配置仍可作为备选。
2. 将更新后的 `monitor.py`、`config.json`、`.gitignore` 和 `.github/workflows/monitor.yml` 推送到目标分支。不要提交密钥。
3. 在 Actions 手动选择 `force` 试跑一次。确认日志以「卖家精灵 MCP V2」开头，且仓库产生 `data/sellersprite_history.json`。工作流保留约每 4 小时的门控和现有触发方式。

若 WorkBuddy 定时任务与 GitHub Actions 都向同一个飞书群发送报告，建议只保留一套竞品推送，避免重复通知。

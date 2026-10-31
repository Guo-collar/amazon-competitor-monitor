# 亚马逊竞品监控（云端部署版）

监控竞争对手 ASIN 的价格、库存、BSR 排名、评分、评论数变化，异常时预警。基于 GitHub Actions 定时运行，**关机也能持续监控**。

## 监控对象

| 名称 | ASIN | 产品 |
|------|------|------|
| 竞品A | B0C8TG35BF | DownyPaws 宠物饮水机 84oz/2.5L |
| 竞品B | B0DJVX1BX6 | 无线不锈钢宠物饮水机 108oz/3.2L |

## 监控维度

价格、划线原价/折扣、库存状态、大类 BSR、小类 BSR、评分、评论数。

## 预警规则（config.json 可调）

- 价格变动 ≥ $0.5 或 3%
- 促销/折扣变化（上线、力度变化、结束）
- 库存变化 / 售罄 / 回货
- 大类 BSR 变化 ≥ 20%
- 小类 BSR 变化 ≥ 15% 或名次变化 ≥3
- 评分变化 ≥ 0.1 星
- 评论数激增 ≥ 20%

## 部署步骤（GitHub Actions）

### 1. 创建 GitHub 仓库并推送代码

```bash
cd "D:/桌面/Workbuddy (3)/亚马逊/监控竞争对手"
git init
git add .
git commit -m "init: 亚马逊竞品监控"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

> 仓库名随意，例如 `amazon-competitor-monitor`。仓库可以是私有的（Private），更安全。

### 2. 配置邮件预警（可选）

在 GitHub 仓库页 `Settings → Secrets and variables → Actions → New repository secret` 添加以下 Secrets：

| Secret 名称 | 说明 | 示例 |
|------------|------|------|
| `SMTP_HOST` | SMTP 服务器 | `smtp.qq.com` |
| `SMTP_PORT` | 端口（465=SSL，587=STARTTLS） | `465` |
| `SMTP_USER` | 发件邮箱 | `you@qq.com` |
| `SMTP_PASS` | SMTP 授权码（非登录密码） | `xxxxxxxx` |
| `ALERT_TO` | 收件邮箱 | `you@qq.com` |

> **如何获取 SMTP 授权码**（以 QQ 邮箱为例）：设置 → 账户 → 开启 SMTP 服务 → 生成授权码。
> 不配置 Secrets 也能运行，只是异常时不会发邮件，预警记录会保存在仓库 `data/alerts.jsonl` 里。

### 3. 启用定时任务

推送代码后，GitHub Actions 的定时任务（`.github/workflows/monitor.yml` 里的 `cron: 0 */6 * * *`）会自动生效，每 6 小时运行一次。

也可以在仓库 `Actions` 页手动点「Run workflow」立即测试一次。

### 4. 查看运行结果

- 仓库 `Actions` 页查看每次运行日志
- `data/history.json` — 最新数据快照（自动提交回仓库，实现历史持久化）
- `data/alerts.jsonl` — 预警记录

## 技术要点

- 用 `curl` 抓取（Python urllib/requests 会被亚马逊反爬拦截）
- `i18n-prefs=USD` cookie 确保返回美元价格
- 历史快照通过 commit 回仓库实现跨运行持久化（GitHub runner 无状态）

## 常见问题

**Q: 为什么用 GitHub Actions 免费额度够吗？**
A: 私有仓库每月 2000 分钟，本项目每 6 小时跑 1 次、每次约 1 分钟，每月约 120 分钟，完全够用。

**Q: 亚马逊会不会封 IP？**
A: 每 6 小时才抓 2 个页面，频率极低，风险很小。如需更频繁可自行调 cron。

**Q: 想加新竞品？**
A: 编辑 `config.json` 的 `asins` 数组，添加 `{"asin": "xxx", "name": "竞品C"}` 即可。

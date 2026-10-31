# 外部定时触发器部署指南（Cloudflare Worker）

## 为什么需要这个

GitHub Actions 自带的定时任务（`schedule: cron`）在这个仓库上**完全不触发**。

实测证据：

| 时间（北京） | 本该触发 | 实际 |
|---|---|---|
| 14:00 | `0 */6 * * *` | 到 14:43 仍无，`?event=schedule` 返回 `total_count=0` |
| 14:30 | `*/30 * * * *` | 到 14:43 仍无 |
| 14:47 | 临时改成每分钟 `* * * * *` | 等 6 分钟，仍然 0 次 |

仓库本身没问题（非 fork、未归档、未禁用、默认分支 main、Actions 状态 active）。
结论：GitHub 的免费 cron 对这个仓库不调度。官方文档也写明定时任务不保证准时，
高负载时会延迟甚至直接丢弃。

所以改用 **Cloudflare 的 Cron Trigger 当外部闹钟**：它准时、免费，
每 30 分钟调一次 GitHub 的 `workflow_dispatch` 接口。抓取、对比、飞书推送仍然全部在
GitHub 上跑，已跑通的逻辑一行都不用改。

`monitor.py` 内部有 6 小时门控（读 `data/last_run.json`），所以外部每 30 分钟戳一次
不会造成重复推送——只有距上次实际执行超过 5.8 小时才会真正抓取并推送。

---

## 需要准备的东西

一共两个：一个 GitHub Token、一个 Cloudflare 账号（免费）。全程约 10 分钟。

本仓库对应的参数，后面直接抄：

| 参数 | 值 |
|---|---|
| `REPO` | `Guo-collar/amazon-competitor-monitor` |
| `WORKFLOW_ID` | `402402553` |
| `BRANCH` | `main` |

---

## 第一步：创建 GitHub Token

用 Fine-grained token，只给这一个仓库的最小权限，比 classic token 安全得多。

1. 打开 https://github.com/settings/tokens?type=beta
2. 点右上角 **Generate new token**
3. 填写：
   - **Token name**：`cf-monitor-cron`
   - **Expiration**：选 `Custom`，填一年后（到期需要重新生成，建议记个提醒）
   - **Repository access**：选 **Only select repositories** → 勾选 `amazon-competitor-monitor`
4. 展开 **Permissions** → **Repository permissions**，只改一项：
   - **Actions** → 设为 **Read and write**
   - （其他全部留 No access）
5. 点 **Generate token**，复制生成的 `github_pat_...`

> 这个 Token 只在 Cloudflare 的加密环境变量里保存，不会出现在代码或日志中。

---

## 第二步：创建 Cloudflare Worker

1. 打开 https://dash.cloudflare.com/ 注册（免费，不需要信用卡，不需要域名）
2. 左侧菜单 → **Compute (Workers & Pages)** → **Create**
3. 选 **Start with Hello World!** → **Deploy**
4. 部署后点 **Edit code**（或 Continue to project → Edit code）
5. 把编辑器里默认的代码**全部删掉**，粘贴本目录 `worker.js` 的完整内容
6. 右上角 **Deploy**

---

## 第三步：配置环境变量

在 Worker 页面 → **Settings** → **Variables and Secrets** → **Add**，逐个添加（类型选 **Secret**）：

| 变量名 | 值 |
|---|---|
| `GITHUB_TOKEN` | 第一步复制的 `github_pat_...` |
| `REPO` | `Guo-collar/amazon-competitor-monitor` |
| `WORKFLOW_ID` | `402402553` |
| `BRANCH` | `main` |

加完点 **Deploy** 让变量生效。

> 如果用的是新版 Dashboard，可能叫 **Settings → Variables**，或分成
> "Environment Variables" 和 "Secrets" 两块，Secret 的加在 Secrets 里即可。

---

## 第四步：设置定时器

Worker 页面 → **Settings** → **Triggers**（旧版可能叫 **Triggers → Cron Triggers**）

1. 点 **Add Cron Trigger**
2. 填：`*/30 * * * *`
3. 保存

含义：每 30 分钟的 0 分和 30 分各触发一次。

---

## 第五步：验证

**方法一（最快）**：直接用浏览器打开你的 Worker 地址
（形如 `https://<worker名>.<你的子域>.workers.dev`）

- 返回 `{"ok": true, "status": 204, "message": "已触发 GitHub 工作流"}`
  → 配置正确，GitHub 已被唤醒
- 返回 `ok: false` 并带 `status: 401/403/404`
  → 见下面的排查表

**方法二**：打开 https://github.com/Guo-collar/amazon-competitor-monitor/actions
看是否出现一条新的 `workflow_dispatch` 运行记录。

触发后两种情况都算正常：

- 距上次执行不足 5.8 小时 → 日志显示 `⏭ 定时门控：距上次运行仅 x.xx 小时（周期 5.8 小时），跳过本轮`
  然后立刻结束，**不推送**（这是正确的，避免刷屏）
- 距上次执行超过 5.8 小时 → 正常抓取，飞书收到巡检卡片

---

## 常见错误排查

| 返回 | 原因 | 处理 |
|---|---|---|
| `401 Bad credentials` | Token 填错或有空格 | 重新复制 Token，注意首尾不要带空格 |
| `403 Resource not accessible` | Token 权限不足 | 确认 Fine-grained token 的 **Actions** 权限是 **Read and write**，且仓库已勾选 |
| `404 Not Found` | `REPO` 拼错，或 `WORKFLOW_ID` 不对 | 核对上表；`WORKFLOW_ID` 是数字 ID，不是 `monitor.yml` |
| `422 Workflow does not have 'workflow_dispatch' trigger` | workflow 文件里缺 `workflow_dispatch` | 本仓库已有，无需处理 |
| 一直没反应 | Cron Trigger 没保存成功 | 回 Triggers 页面确认已列出 `*/30 * * * *` |

Token 过期（一年后）会开始返回 401，届时重新生成一个替换 `GITHUB_TOKEN` 即可。

---

## 验证定时器真的在跑

Cloudflare Dashboard → Worker → **Logs**（实时日志），等下一个整点/半点，
应该能看到一条 `cron=*/30 * * * * ok=true status=204`。

GitHub 侧则在 Actions 页面看到每 30 分钟出现一条秒退的运行记录
（只有每 6 小时那一次会真正抓取并推送）。这是预期行为。

# 飞书 ↔ GitHub Actions 双向交互机器人

让你在飞书群里发一句「抓取」，云端立刻执行一次竞品监控。

部署一次，以后随时能用。

---

## 它怎么工作

```
飞书群发「抓取」
   ↓ 飞书事件订阅推送到 Cloudflare Worker
Worker 解析指令
   ↓ 调 GitHub API
GitHub Actions 执行 monitor.py
   ↓ 抓取完毕，用飞书 Webhook 推送
飞书群收到预警卡片
```

现有的 `FEISHU_WEBHOOK`（自定义机器人）继续负责"发"，这个 Worker 负责"收"。

---

## 上车前的三个准备

| 需要什么 | 去哪弄 | 说明 |
|---------|--------|------|
| GitHub Token | GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens | 权限只需 **Actions: Read and write**，仓库选 `amazon-competitor-monitor` |
| 飞书自建应用 | https://open.feishu.cn/app | 需要飞书管理员权限，个人可先创建一个"团队" |
| Cloudflare 账号 | https://dash.cloudflare.com/sign-up | 免费，无需信用卡 |

---

## 第一步：创建飞书自建应用

1. 打开 https://open.feishu.cn/app → 点「创建企业自建应用」
2. 填名称（如 `亚马逊监控助手`）、描述，点创建
3. 左侧「**凭证与基础信息**」页，记下两个值：
   - **App ID**（`cli_xxxxxxxx`）
   - **App Secret**
4. 左侧「**添加应用能力**」→ 找到「**机器人**」→ 点「添加」
5. 左侧「**权限管理**」→ 搜索并开通以下权限：
   - `im:message` —— 获取与发送单聊、群组消息
   - `im:message:send_as_bot` —— 以应用身份发消息
   - `im:chat` —— 获取群组信息
6. 左侧「**事件订阅**」（先别填地址，等 Worker 部署好再回来）：
   - 记下 **Verification Token**
   - 记下 **Encrypt Key**（点「重置」生成一个）
   - 添加事件：`im.message.receive_v1`（接收消息）

---

## 第二步：部署 Cloudflare Worker

1. 打开 https://dash.cloudflare.com → 左侧「**Workers & Pages**」→「Create」→「Worker」
2. 起个名字（如 `feishu-github-bot`）→ Deploy
3. 点「**Edit code**」，把本目录 `worker.js` 的**全部内容**粘贴进去，覆盖原有代码
4. 点右上角「**Deploy**」
5. 回到 Worker 详情页 →「**Settings**」→「**Variables and Secrets**」，添加这些变量（全部选 **Secret** 类型）：

| 变量名 | 值 |
|--------|-----|
| `FEISHU_APP_ID` | 第一步的 App ID |
| `FEISHU_APP_SECRET` | 第一步的 App Secret |
| `FEISHU_ENCRYPT_KEY` | 第一步的 Encrypt Key |
| `FEISHU_VERIFICATION_TOKEN` | 第一步的 Verification Token |
| `GITHUB_TOKEN` | 你的 GitHub Token |
| `GITHUB_REPO` | `Guo-collar/amazon-competitor-monitor` |

6. 记下 Worker 的访问地址，形如：
   `https://feishu-github-bot.你的子域.workers.dev`

先验证一下能不能访问：浏览器打开这个地址，应该显示 `feishu-github-bot alive`。

---

## 第三步：回填事件订阅地址

回到飞书应用的「**事件订阅**」页面：

- **请求地址**填：`https://feishu-github-bot.你的子域.workers.dev/feishu/event`
- 点「保存」，飞书会立刻发一个验证请求
- 显示「**验证成功**」才对

如果提示失败，检查：
- Worker 是否已部署（浏览器能否访问到 alive 页面）
- `FEISHU_ENCRYPT_KEY` 和 `FEISHU_VERIFICATION_TOKEN` 是否填对
- Worker 里有没有报错（Cloudflare 的 Logs 面板可看）

---

## 第四步：发布应用并拉进群

1. 飞书应用左侧「**版本管理与发布**」→「创建版本」→ 填版本号 `1.0.0` → 保存 → 申请发布
2. 如果提示需要审核，用管理员账号进入「管理后台 → 工作台 → 应用审核」自己通过
3. 回到飞书群：右上角设置 → 群机器人 → 添加机器人 → 找到你创建的应用 → 添加

---

## 第五步：验证

在群里 **@机器人** 发送：

```
抓取
```

机器人应立刻回复「✅ 已触发实时抓取，约 30-60 秒后结果会发到群里」。

## 可用指令

| 指令 | 动作 |
|------|------|
| `抓取` / `监控` / `运行` | 执行一次实时监控（有异常才推预警） |
| `测试` | 只发一条测试卡片，验证推送链路 |
| 其他任意文字 | 回复指令帮助 |

> 注意：必须 **@机器人** 才会触发，因为飞书事件订阅默认只推送 @机器人 的群消息。

---

## 常见问题

**机器人不回复**
- 检查 Worker 的 Logs（Cloudflare → Worker → Logs）有没有收到请求
- 事件订阅地址是否正确、是否验证成功
- 应用是否已发布并添加进群

**提示「触发失败」**
- `GITHUB_TOKEN` 权限不足，需要 Actions: Read and write
- Token 过期（细粒度令牌可设置有效期）

**飞书返回 400 / 解密失败**
- `FEISHU_ENCRYPT_KEY` 填错，注意不要带空格

---

## 成本

Cloudflare Workers 免费额度：每天 10 万次请求。你一天最多发几十条指令，用不到百分之一。

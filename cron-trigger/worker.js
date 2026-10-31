/**
 * 亚马逊竞品监控 —— 外部定时触发器（Cloudflare Worker）
 *
 * 作用：只做"闹钟"。定时调用 GitHub Actions 的 workflow_dispatch 接口，
 *      让云端跑 monitor.py。抓取、对比、飞书推送全部仍在 GitHub 上完成。
 *
 * 为什么需要它：GitHub Actions 自带的 schedule（cron）对部分仓库完全不调度，
 *              实测本仓库 schedule 触发次数长期为 0，改成每分钟 cron 也不响。
 *              Cloudflare 的 Cron Trigger 准时且免费，用它当外部闹钟最省事。
 *
 * 部署要点（详见同目录 README.md）：
 *   环境变量 GITHUB_TOKEN —— Fine-grained PAT，仅授权本仓库的 Actions: Read and write
 *   环境变量 REPO         —— 例如 Guo-collar/amazon-competitor-monitor
 *   环境变量 WORKFLOW_ID  —— workflow 数字 ID（不是文件名），例如 402402553
 *   触发器 Cron           —— 每 30 分钟（在 Dashboard 的 Triggers 里填）
 *
 * 每 30 分钟唤醒一次，GitHub 侧脚本内部有 6 小时门控，
 * 只有真正到达周期才抓取并推送，所以这里频繁触发不会造成重复推送。
 */

const API_BASE = "https://api.github.com";

async function dispatchWorkflow(env) {
  const repo = env.REPO;
  const workflowId = env.WORKFLOW_ID;
  const branch = env.BRANCH || "main";

  if (!env.GITHUB_TOKEN) throw new Error("缺少环境变量 GITHUB_TOKEN");
  if (!repo) throw new Error("缺少环境变量 REPO");
  if (!workflowId) throw new Error("缺少环境变量 WORKFLOW_ID");

  const url = `${API_BASE}/repos/${repo}/actions/workflows/${workflowId}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "cf-monitor-cron",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: branch }),
  });

  const text = await res.text();
  // GitHub 成功时返回 204 且 body 为空
  if (res.status === 204) {
    return { ok: true, status: res.status, message: "已触发 GitHub 工作流" };
  }
  let detail = text;
  try {
    const j = JSON.parse(text);
    detail = j.message || text;
  } catch (e) {
    /* 保持原始文本 */
  }
  return { ok: false, status: res.status, message: detail };
}

export default {
  // Cron Trigger 入口
  async scheduled(event, env, ctx) {
    const r = await dispatchWorkflow(env);
    const line = `${new Date().toISOString()} cron=${event.cron} ok=${r.ok} status=${r.status} ${r.message}`;
    if (r.ok) {
      console.log(line);
    } else {
      console.error(line);
    }
  },

  // 手动测试入口：浏览器访问 Worker 地址即立刻触发一次，方便验证配置
  async fetch(request, env, ctx) {
    const r = await dispatchWorkflow(env);
    return new Response(
      JSON.stringify(
        {
          ok: r.ok,
          status: r.status,
          message: r.message,
          hint: r.ok
            ? "已触发，去 GitHub 仓库的 Actions 页面看最新一次运行"
            : "触发失败，检查 GITHUB_TOKEN 权限、REPO 和 WORKFLOW_ID 是否正确",
        },
        null,
        2
      ),
      {
        status: r.ok ? 200 : 502,
        headers: { "Content-Type": "application/json; charset=utf-8" },
      }
    );
  },
};

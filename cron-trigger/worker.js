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
 *   唯一必须配置的环境变量：GITHUB_TOKEN
 *       —— Fine-grained PAT，仅授权 amazon-competitor-monitor 仓库的 Actions: Read and write
 *   触发器 Cron：每 30 分钟（在 Dashboard 的 Triggers 里填）
 *
 * 仓库名 / 工作流 ID / 分支已内置在下方 CONFIG 中，无需再配环境变量。
 * 若日后换了仓库，改 CONFIG 里的值即可，或者用同名环境变量覆盖。
 *
 * 每 30 分钟唤醒一次，GitHub 侧脚本内部有 4 小时门控，
 * 只有真正到达周期才抓取并推送，所以这里频繁触发不会造成重复推送。
 */

const API_BASE = "https://api.github.com";

// 内置配置：这些都不是敏感信息，写死可以少配 3 个环境变量
const CONFIG = {
  REPO: "Guo-collar/amazon-competitor-monitor",
  WORKFLOW_ID: "402402553",
  BRANCH: "main",
};

function resolveConfig(env) {
  // 环境变量优先，便于以后换仓库时不动代码
  return {
    repo: env.REPO || CONFIG.REPO,
    workflowId: env.WORKFLOW_ID || CONFIG.WORKFLOW_ID,
    branch: env.BRANCH || CONFIG.BRANCH,
    token: env.GITHUB_TOKEN || "",
  };
}

async function dispatchWorkflow(env) {
  const cfg = resolveConfig(env);

  if (!cfg.token) {
    return {
      ok: false,
      status: 0,
      message:
        "缺少环境变量 GITHUB_TOKEN。请到 Worker 的 Settings → Variables and Secrets 添加，" +
        "值为 github_pat_ 开头的完整 Token。",
    };
  }
  if (!cfg.token.startsWith("github_pat_") && !cfg.token.startsWith("ghp_")) {
    return {
      ok: false,
      status: 0,
      message:
        "GITHUB_TOKEN 格式不对（当前长度 " +
        cfg.token.length +
        "，开头不是 github_pat_）。请重新生成 Fine-grained token 并完整复制整串（约 93 个字符）。",
    };
  }

  const url = `${API_BASE}/repos/${cfg.repo}/actions/workflows/${cfg.workflowId}/dispatches`;
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cfg.token}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "cf-monitor-cron",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: cfg.branch }),
    });
  } catch (e) {
    return {
      ok: false,
      status: 0,
      message: "请求 GitHub 失败：" + (e && e.message ? e.message : e),
    };
  }

  const text = await res.text();
  // GitHub 成功时返回 204 且 body 为空
  if (res.status === 204) {
    return {
      ok: true,
      status: 204,
      message: "已成功触发 GitHub 工作流",
      hint: "打开 https://github.com/" + cfg.repo + "/actions 可看到最新一次运行",
    };
  }

  let detail = text;
  try {
    const j = JSON.parse(text);
    detail = j.message || text;
  } catch (e) {
    /* 保持原始文本 */
  }

  const advice = {
    401: "Token 无效。检查是否复制完整、首尾有无空格，或已过期/已删除。",
    403: "Token 权限不足。确认 Fine-grained token 的 Actions 权限为 Read and write。",
    404: "找不到仓库或工作流。检查 REPO / WORKFLOW_ID 是否正确。",
    422: "该工作流没有 workflow_dispatch 触发器。",
  }[res.status];

  return {
    ok: false,
    status: res.status,
    message: detail,
    hint: advice || "参考 GitHub 返回的错误信息排查",
  };
}

function jsonResponse(payload, status) {
  return new Response(JSON.stringify(payload, null, 2), {
    status: status || 200,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

export default {
  // Cron Trigger 入口
  async scheduled(event, env, ctx) {
    try {
      const r = await dispatchWorkflow(env);
      const line =
        new Date().toISOString() +
        " cron=" + event.cron +
        " ok=" + r.ok +
        " status=" + r.status +
        " " + r.message;
      if (r.ok) {
        console.log(line);
      } else {
        console.error(line);
      }
    } catch (e) {
      console.error(
        new Date().toISOString() + " 定时触发异常：" + (e && e.message ? e.message : e)
      );
    }
  },

  // 手动测试入口：浏览器打开 Worker 地址即立刻触发一次
  // 加 ?check=1 只检查配置、不触发，方便排查
  async fetch(request, env, ctx) {
    try {
      const url = new URL(request.url);
      const cfg = resolveConfig(env);

      if (url.searchParams.get("check") === "1") {
        return jsonResponse({
          ok: Boolean(cfg.token),
          说明: "仅检查配置，未触发 GitHub",
          Token已配置: Boolean(cfg.token),
          Token长度: cfg.token.length,
          Token开头: cfg.token ? cfg.token.slice(0, 11) : "(空)",
          仓库: cfg.repo,
          工作流ID: cfg.workflowId,
          分支: cfg.branch,
        });
      }

      const r = await dispatchWorkflow(env);
      const body = {
        ok: r.ok,
        status: r.status,
        message: r.message,
        仓库: cfg.repo,
        工作流ID: cfg.workflowId,
      };
      if (r.hint) body.hint = r.hint;
      return jsonResponse(body, r.ok ? 200 : 502);
    } catch (e) {
      return jsonResponse(
        {
          ok: false,
          error: e && e.message ? e.message : String(e),
          hint: "Worker 内部异常，把这段内容发给助手排查",
        },
        500
      );
    }
  },
};

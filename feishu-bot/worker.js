/**
 * 飞书 ↔ GitHub Actions 双向交互机器人
 * 部署平台：Cloudflare Workers（免费版足够）
 *
 * 作用：在飞书群里发「抓取」，机器人调 GitHub API 触发 workflow_dispatch，
 *      抓取结果再由 monitor.py 通过飞书 webhook 推回群里。
 *
 * 环境变量（在 Cloudflare Worker 的 Settings -> Variables 里配置）：
 *   FEISHU_APP_ID            飞书自建应用 App ID（cli_xxx）
 *   FEISHU_APP_SECRET        飞书自建应用 App Secret
 *   FEISHU_ENCRYPT_KEY       事件订阅的 Encrypt Key（开启加密时必填）
 *   FEISHU_VERIFICATION_TOKEN 事件订阅的 Verification Token
 *   GITHUB_TOKEN             GitHub 细粒度 PAT，权限：Actions = Read and write
 *   GITHUB_REPO              Guo-collar/amazon-competitor-monitor
 *   GITHUB_WORKFLOW          可选，默认 monitor.yml
 *   GITHUB_REF               可选，默认 main
 */

let cachedToken = null;
let tokenExpireAt = 0;

export default {
  async fetch(request, env, ctx) {
    if (request.method === 'GET') {
      return new Response('feishu-github-bot alive', { status: 200 });
    }
    if (request.method !== 'POST') {
      return jsonResp({ code: 1, msg: 'method not allowed' }, 405);
    }

    let body;
    try {
      body = await request.json();
    } catch (e) {
      return jsonResp({ code: 1, msg: 'invalid json' }, 400);
    }

    // 飞书加密模式：body.encrypt 是密文，需要解密
    if (body.encrypt) {
      if (!env.FEISHU_ENCRYPT_KEY) {
        return jsonResp({ code: 1, msg: 'missing FEISHU_ENCRYPT_KEY' }, 500);
      }
      try {
        body = JSON.parse(await decrypt(body.encrypt, env.FEISHU_ENCRYPT_KEY));
      } catch (e) {
        return jsonResp({ code: 1, msg: 'decrypt failed: ' + e.message }, 400);
      }
    }

    // 1. URL 验证（配置事件订阅地址时飞书会发这个）
    if (body.type === 'url_verification') {
      return jsonResp({ challenge: body.challenge });
    }

    // 2. 校验 Verification Token，防止伪造请求
    const token = body.header?.token || body.token;
    if (env.FEISHU_VERIFICATION_TOKEN && token !== env.FEISHU_VERIFICATION_TOKEN) {
      return jsonResp({ code: 1, msg: 'invalid verification token' }, 403);
    }

    // 3. 消息事件
    if (body.header?.event_type === 'im.message.receive_v1') {
      const message = body.event?.message || {};
      const chatId = message.chat_id;
      const text = extractText(message);
      // 飞书要求 3 秒内响应，业务逻辑放后台异步执行
      ctx.waitUntil(handleCommand(text, chatId, env));
    }

    return jsonResp({ code: 0 });
  },
};

/** 从飞书消息体中提取纯文本，去掉 @机器人 的占位符 */
function extractText(message) {
  if (message.message_type !== 'text') return '';
  try {
    const content = JSON.parse(message.content || '{}');
    return (content.text || '').replace(/@_user_\d+/g, '').trim();
  } catch (e) {
    return '';
  }
}

/** 解析指令并触发 */
async function handleCommand(text, chatId, env) {
  const t = text.replace(/\s/g, '');
  let testMode = null;

  if (/测试|test/i.test(t)) {
    testMode = true;
  } else if (/抓取|监控|运行|刷新|更新|run|check/i.test(t)) {
    testMode = false;
  } else {
    await reply(
      chatId,
      '可用指令：\n• 抓取 —— 立即执行一次实时监控（约 30-60 秒）\n• 测试 —— 只发一条测试消息，验证推送是否正常',
      env
    );
    return;
  }

  const ok = await triggerWorkflow(testMode, env);
  if (ok) {
    await reply(
      chatId,
      testMode
        ? '✅ 已触发测试推送，几秒后结果会发到群里。'
        : '✅ 已触发实时抓取，约 30-60 秒后结果会发到群里（无异常则不推送）。',
      env
    );
  } else {
    await reply(chatId, '❌ 触发失败，请检查 GITHUB_TOKEN 权限是否包含 Actions: write。', env);
  }
}

/** 调用 GitHub API 触发 workflow_dispatch */
async function triggerWorkflow(testMode, env) {
  const repo = env.GITHUB_REPO;
  const wf = env.GITHUB_WORKFLOW || 'monitor.yml';
  const ref = env.GITHUB_REF || 'main';
  if (!repo || !env.GITHUB_TOKEN) return false;

  const res = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${wf}/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'feishu-github-bot',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        ref,
        inputs: { test_email: testMode ? 'true' : 'false' },
      }),
    }
  );
  return res.status === 204;
}

/** 获取 tenant_access_token（带内存缓存） */
async function getTenantToken(env) {
  if (cachedToken && Date.now() < tokenExpireAt) return cachedToken;

  const res = await fetch(
    'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({
        app_id: env.FEISHU_APP_ID,
        app_secret: env.FEISHU_APP_SECRET,
      }),
    }
  );
  const data = await res.json();
  if (data.code !== 0) return null;

  cachedToken = data.tenant_access_token;
  tokenExpireAt = Date.now() + Math.max(60, (data.expire || 7200) - 300) * 1000;
  return cachedToken;
}

/** 在群里回复文本消息 */
async function reply(chatId, text, env) {
  if (!chatId) return;
  const token = await getTenantToken(env);
  if (!token) return;

  await fetch(
    'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json; charset=utf-8',
      },
      body: JSON.stringify({
        receive_id: chatId,
        msg_type: 'text',
        content: JSON.stringify({ text }),
      }),
    }
  );
}

/** 飞书事件解密：AES-256-CBC，key = SHA256(encrypt_key)，前 16 字节为 IV */
async function decrypt(encrypt, encryptKey) {
  const enc = new TextEncoder();
  const keyHash = await crypto.subtle.digest('SHA-256', enc.encode(encryptKey));
  const raw = Uint8Array.from(atob(encrypt), (c) => c.charCodeAt(0));
  const iv = raw.slice(0, 16);
  const ciphertext = raw.slice(16);
  const key = await crypto.subtle.importKey('raw', keyHash, { name: 'AES-CBC' }, false, [
    'decrypt',
  ]);
  const plain = await crypto.subtle.decrypt({ name: 'AES-CBC', iv }, key, ciphertext);
  return new TextDecoder().decode(plain);
}

function jsonResp(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
}

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
亚马逊竞争对手监控脚本（云端部署版）
监控多个 ASIN 的价格、库存（接口提供时）、BSR（大类+小类）、评分和评分数。
正式数据源仅使用卖家精灵 MCP V2，不回退到亚马逊网页。

用法:
  python monitor.py             # 取数、对比并生成巡检报告
  python monitor.py --init      # 仅建立基线，不推送
  python monitor.py --dry-run   # 取数及预览，不写入文件、不推送

推送方式（可选，按优先级尝试）：
  1. 飞书自定义机器人（推荐，卡片消息）：FEISHU_WEBHOOK（+ 可选 FEISHU_SECRET 签名校验）
  2. Server酱（微信推送）：SERVERCHAN_KEY
  3. 企业微信/钉钉 群机器人：WEBHOOK_URL
  4. 邮件 SMTP：SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / ALERT_TO
  都未配置时仅写日志和 alerts.jsonl。

注意：QQ 邮箱 SMTP 无法从 GitHub Actions（美国节点）访问，云端请用飞书机器人 / Server酱。
飞书群机器人创建：飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook 地址
（安全设置里勾选「签名校验」时，把密钥填到 FEISHU_SECRET；勾选「自定义关键词」时填「亚马逊」）

数据持久化（云端）：
  本脚本支持两种持久化方式：
  1. 本地文件（默认，data/history.json）
  2. 通过 HISTORY_STATE 环境变量传入/传出历史快照（用于 GitHub Actions 无状态环境）
     运行结束后会把最新历史快照打印为可粘贴的 JSON，供下次运行时通过环境变量回传。
"""
import json
import os
import re
import sys
import time
import base64
import hashlib
import hmac
import datetime
import subprocess
import tempfile
import smtplib
import urllib.request
import urllib.parse
from pathlib import Path
from email.mime.text import MIMEText
from email.header import Header

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "sellersprite_history.json")
LAST_RUN_PATH = os.path.join(DATA_DIR, "sellersprite_last_run.json")
DRY_RUN = "--dry-run" in sys.argv
ALERTS_PATH = os.path.join(DATA_DIR, "alerts.jsonl")
LOG_PATH = os.path.join(DATA_DIR, "monitor.log")

# 定时门控参数
# GitHub Actions 的 schedule 触发不可靠：整点排队延迟 5~30 分钟，高负载时直接丢弃。
# 因此由外部定时器（Cloudflare Worker）每 30 分钟唤醒一次，脚本内部判断是否已到周期。
SCHEDULE_INTERVAL_HOURS = 4
SCHEDULE_GATE_HOURS = 3.8  # 略小于周期，保证每个周期恰好执行一次而不会漏过

# 统一使用北京时间（GitHub Actions runner 默认是 UTC，会造成 8 小时时差）
TZ_BEIJING = datetime.timezone(datetime.timedelta(hours=8))


def bjnow(fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.datetime.now(TZ_BEIJING).strftime(fmt)


def log(msg):
    line = f"[{bjnow()}] {msg}"
    print(line)
    if DRY_RUN:
        return
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def schedule_gate(hours=SCHEDULE_GATE_HOURS):
    """定时门控：判断本轮是否已到抓取周期。

    GitHub Actions 的 cron 不保证准时（整点排队常延迟 5~30 分钟，高负载时可能整轮被丢弃），
    所以外部定时器每 30 分钟唤醒一次，用本函数保证真正的抓取仍按 SCHEDULE_INTERVAL_HOURS 的周期执行。
    返回 (should_run, reason)。
    """
    last_ts = None
    if os.path.exists(LAST_RUN_PATH):
        try:
            with open(LAST_RUN_PATH, "r", encoding="utf-8") as f:
                last_ts = json.load(f).get("last_run_ts")
        except Exception as e:
            log(f"  读取运行记录失败: {e}")
    if not last_ts:
        return True, "首次运行（无历史记录）"
    try:
        elapsed_h = (time.time() - float(last_ts)) / 3600
    except (TypeError, ValueError):
        return True, "运行记录时间戳异常，强制执行"
    if elapsed_h >= hours:
        return True, f"距上次运行 {elapsed_h:.2f} 小时，已达 {hours} 小时周期"
    return False, f"距上次运行仅 {elapsed_h:.2f} 小时（周期 {hours} 小时），跳过本轮"


def mark_run():
    """记录本轮实际执行时间，供下一轮门控判断。抓取失败也要记，否则会每 30 分钟重试刷屏。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LAST_RUN_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "last_run_ts": time.time(),
                "last_run_bj": bjnow(),
                "interval_hours": SCHEDULE_INTERVAL_HOURS,
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"  写入运行记录失败: {e}")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# 旧网页解析函数仅为维护历史记录而保留，正式入口不调用。
# 浏览器指纹池：UA 必须与 sec-ch-ua / platform 匹配，否则反而更可疑
_UA_PROFILES = [
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "platform": '"Windows"',
        "mobile": "?0",
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "ch_ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        "platform": '"macOS"',
        "mobile": "?0",
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "ch_ua": '"Microsoft Edge";v="129", "Chromium";v="129", "Not=A?Brand";v="24"',
        "platform": '"Windows"',
        "mobile": "?0",
    },
]

# 反爬页面特征词（命中即判为无效页面）
_BLOCK_MARKERS = (
    "api-services-support@amazon.com",
    "Type the characters you see",
    "Enter the characters you see below",
    "Robot Check",
    "automated access to Amazon data",
    "Sorry, we just need to make sure",
)


def _diag_page(html):
    """输出页面诊断信息，便于判断是验证页、降级页还是改版"""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:70]
    hits = [k for k in _BLOCK_MARKERS if k.lower() in html.lower()]
    return f"长度={len(html)} 标题={title!r} 反爬特征={hits or '无'}"


def _page_ok(html):
    """返回的页面是否真的包含商品与价格（而非降级页/验证页）"""
    if not html or len(html) < 50000:
        return False
    if any(k.lower() in html.lower() for k in _BLOCK_MARKERS):
        return False
    if "productTitle" not in html:
        return False
    return parse_price(html) is not None


def _warmup_session(profile, timeout):
    """先访问首页拿到 session cookie（session-id 等），让后续商品页请求是"有会话的浏览器"。
    实测：云端 IP 直接请求 /dp/ 常被 3781 字节验证页拦截，无 cookie 是重要原因之一。"""
    jar = os.path.join(tempfile.gettempdir(), f"amz_jar_{os.getpid()}.txt")
    try:
        subprocess.run([
            "curl", "-s", "--compressed",
            "-c", jar,
            "-H", f"User-Agent: {profile['ua']}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", "Upgrade-Insecure-Requests: 1",
            "-H", "Sec-Fetch-Dest: document",
            "-H", "Sec-Fetch-Mode: navigate",
            "-H", "Sec-Fetch-Site: none",
            "-H", "Sec-Fetch-User: ?1",
            "-H", f"sec-ch-ua: {profile['ch_ua']}",
            "-H", f"sec-ch-ua-mobile: {profile['mobile']}",
            "-H", f"sec-ch-ua-platform: {profile['platform']}",
            "--max-time", str(min(timeout, 20)),
            "-o", os.devnull,
            "https://www.amazon.com/",
        ], check=False, capture_output=True, timeout=timeout + 5)
        if os.path.exists(jar):
            # 追加币种偏好，确保后续页面是美元
            return jar
    except Exception as e:
        log(f"  [预热] 会话预热失败（忽略）: {type(e).__name__}")
    return None


def fetch_page(asin, config):
    """用 curl 抓取页面（curl 的 TLS 特征能绕过亚马逊机器人检测）

    可靠性设计：
      1. 完整的现代浏览器请求头（含 sec-ch-ua / sec-fetch 系列），缺这些是云端被降级的主因
      2. 先访问首页预热 session cookie，再请求商品页（无会话是 /dp/ 被拦的主因）
      3. 带 Referer，模拟站内跳转
      4. UA 指纹轮换，避免单一指纹被标记
      5. 主站页面失败后改用移动版 /gp/aw/d/ 兜底（但移动版无 BSR 表格，仅作兜底）
      6. 优选含 BSR 的桌面版页面；严格校验：必须同时有 productTitle 与可解析价格
    """
    timeout = config["request"]["timeout"]
    retry = config["request"]["retry"]
    # 第 1 轮用桌面版，后续轮次追加移动版兜底
    url_templates = [
        "https://www.amazon.com/dp/{asin}?currencyOfPreference=USD&language=en_US&th=1&psc=1",
        "https://www.amazon.com/gp/aw/d/{asin}?currencyOfPreference=USD&language=en_US",
    ]
    # 每套指纹各自预热一个会话
    jars = {}
    last_err = None
    best_html = None  # 有效但缺 BSR 的候选页面（兜底用）
    for i in range(retry):
        profile = _UA_PROFILES[i % len(_UA_PROFILES)]
        pk = i % len(_UA_PROFILES)
        if pk not in jars:
            jars[pk] = _warmup_session(profile, timeout)
        jar = jars[pk]
        tpl_list = url_templates if i > 0 else url_templates[:1]
        for ti, tpl in enumerate(tpl_list):
            url = tpl.format(asin=asin)
            tmp = os.path.join(tempfile.gettempdir(), f"amz_{asin}_{os.getpid()}_{i}.html")
            try:
                cmd = [
                    "curl", "-sL", "--compressed",
                    "-H", f"User-Agent: {profile['ua']}",
                    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                    "-H", "Accept-Language: en-US,en;q=0.9",
                    "-H", "Referer: https://www.amazon.com/",
                    "-H", "Upgrade-Insecure-Requests: 1",
                    "-H", "Sec-Fetch-Dest: document",
                    "-H", "Sec-Fetch-Mode: navigate",
                    "-H", "Sec-Fetch-Site: none",
                    "-H", "Sec-Fetch-User: ?1",
                    "-H", f"sec-ch-ua: {profile['ch_ua']}",
                    "-H", f"sec-ch-ua-mobile: {profile['mobile']}",
                    "-H", f"sec-ch-ua-platform: {profile['platform']}",
                    "-H", "sec-gpc: 1",
                    "-H", "Cache-Control: max-age=0",
                    "-b", "i18n-prefs=USD; lc-main=en_US;",
                ]
                # 叠加预热得到的 session cookie（有会话的请求更像真实用户）
                if jar:
                    cmd += ["-b", jar]
                cmd += ["--max-time", str(timeout), "-o", tmp, url]
                subprocess.run(cmd, check=True, capture_output=True, timeout=timeout + 10)
                with open(tmp, "r", encoding="utf-8", errors="ignore") as f:
                    html = f.read()
                if os.path.exists(tmp):
                    os.remove(tmp)

                if _page_ok(html):
                    # 优选「含 BSR 表格的完整页面」：Amazon 对云端 IP 有时返回精简版
                    # （有价格/评分，但省掉 Best Sellers Rank 模块）。遇到精简版不立即返回，
                    # 继续换指纹/路径再试，全部失败才退而使用精简版。
                    has_bsr = parse_bsr(html) is not None and parse_sub_bsr(html) is not None
                    if has_bsr:
                        if i > 0 or ti > 0:
                            log(f"  [{asin}] 第{i+1}次尝试成功（{url.split('/')[3]} 路径，含BSR）")
                        return html
                    if best_html is None:
                        best_html = html
                        log(f"  [{asin}] 第{i+1}次页面有效但缺 BSR（{len(html)}字节），继续尝试其他指纹")
                    else:
                        log(f"  [{asin}] 第{i+1}次页面有效但缺 BSR（{len(html)}字节），已有候选，继续")
                    continue

                last_err = _diag_page(html)
                log(f"  [{asin}] 第{i+1}次无效页面: {last_err}")
                # 保存无效页面样本，便于事后判断是验证页 / 降级页 / 改版
                try:
                    os.makedirs(DATA_DIR, exist_ok=True)
                    with open(os.path.join(DATA_DIR, f"bad_{asin}.html"), "w", encoding="utf-8") as bf:
                        bf.write(html[:8000])
                except Exception:
                    pass
            except Exception as e:
                last_err = e
                log(f"  [{asin}] 第{i+1}次抓取失败: {e}")
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
            time.sleep(2)
        time.sleep(3 + i * 4)  # 轮次间退避：3s / 7s / 11s

    if best_html is not None:
        log(f"  [{asin}] ⚠ 未取得含 BSR 的完整页面，使用候选页（价格可用，BSR 将沿用旧值）")
        return best_html
    raise RuntimeError(f"抓取失败（{retry}轮全无效）: {last_err}")


def parse_price(html):
    m = re.findall(r'a-offscreen">\s*\$([0-9]+(?:\.[0-9]{1,2})?)', html)
    if m:
        return float(m[0])
    return None


def parse_list_price(html, current_price=None):
    """划线原价。规则：
    1) 必须高于现价（划线价低于现价是解析错误）
    2) 取最接近现价的候选（页面里还有其他商品/套装的划线价）
    3) 不超过现价 5 倍（超过基本是抓到了别的商品）
    """
    candidates = re.findall(
        r'data-a-strike="true"[^>]*>\s*<span class="a-offscreen">\s*\$([0-9]+(?:\.[0-9]{1,2})?)',
        html
    )
    if not candidates:
        return None
    if current_price is None:
        return float(candidates[0])
    valid = [float(c) for c in candidates if float(c) > current_price]
    if not valid:
        return None
    best = min(valid)  # 最接近现价的合法值
    if best > current_price * 5:
        return None
    return best


def parse_discount(html):
    m = re.search(r'([0-9]+)%\s*off', html, re.I)
    if m:
        return int(m.group(1))
    return None


def parse_stock(html):
    if re.search(r'out of stock|Currently unavailable|temporarily out', html, re.I):
        return "Out of Stock"
    m = re.search(r'[Oo]nly (\d+)\s+left in stock', html)
    if m:
        return f"Only {m.group(1)} left in stock"
    if re.search(r'In Stock', html):
        return "In Stock"
    return "Unknown"


def parse_bsr(html):
    """大类 Best Seller Rank（zg_ts）

    页面实测结构（2026-11）：
      <li><span class="a-list-item"><span>#3,877 in Pet Supplies
          (<a href='/gp/bestsellers/pet-supplies/ref=pd_zg_ts_pet-supplies'>See Top 100...</a>)</span></span></li>
    注意 zg_ts 出现在链接 ref 里、后面紧跟 "_类目名" 而不是 '">'，
    所以不能用 'zg_ts">#N' 这种写法，改为围绕 Best Sellers Rank 表格定位。
    """
    # 优先：限定在 Best Sellers Rank 明细表内，避免抓到推荐位商品的排名
    window = html
    idx = html.find("Best Sellers Rank")
    if idx > 0:
        window = html[idx: idx + 4000]
    m = re.search(r'#([0-9,]+)\s+in\s+([A-Za-z][^<()]{1,60}?)\s*(?:\(|</)', window)
    if m:
        return {"rank": int(m.group(1).replace(",", "")), "category": m.group(2).strip()}
    # 兜底：全页任意 "#N in 某类目"
    m = re.search(r'#([0-9,]+)\s+in\s+([A-Za-z][^<()]{1,60}?)\s*(?:\(|</)', html)
    if m:
        return {"rank": int(m.group(1).replace(",", "")), "category": m.group(2).strip()}
    return None


def parse_sub_bsr(html):
    """小类 Best Seller Rank（zg_hrsr）

    页面实测结构（2026-11）：
      <li><span class="a-list-item"><span>#37 in
          <a href='/gp/bestsellers/pet-supplies/2975263011/ref=pd_zg_hrsr_pet-supplies'>Cat Fountains</a></span></span></li>
    关键特征：排名数字后面直接跟 <a> 链接，链接 href 里含 zg_hrsr。
    """
    m = re.search(r'#([0-9,]+)\s+in\s+<a[^>]*zg_hrsr[^>]*>([^<]+)</a>', html)
    if m:
        return {"rank": int(m.group(1).replace(",", "")), "category": m.group(2).strip()}
    # 兼容旧结构：zg_hrsr 后接 a-list-item 列表
    m = re.search(r'zg_hrsr[^>]*>\s*<li><span class="a-list-item">\s*(?:<span>)?#([0-9,]+)\s+in\s+<a[^>]*>([^<]+)', html)
    if m:
        return {"rank": int(m.group(1).replace(",", "")), "category": m.group(2).strip()}
    return None


def diagnose_bsr(html):
    """BSR 解析失败时输出可定位的诊断信息（云端页面与本地不同，必须能把证据打出来）"""
    bits = [f"长度={len(html)}"]
    bits.append(f"含'Best Sellers Rank'={'Best Sellers Rank' in html}")
    bits.append(f"zg_ts出现{html.count('zg_ts')}次")
    bits.append(f"zg_hrsr出现{html.count('zg_hrsr')}次")
    idx = html.find("Best Sellers Rank")
    if idx > 0:
        seg = re.sub(r"\s+", " ", html[idx: idx + 500])
        bits.append(f"片段={seg[:340]!r}")
    return " | ".join(bits)


def parse_title(html):
    m = re.search(r'<span id="productTitle"[^>]*>\s*([^<]+)', html)
    if m:
        return m.group(1).strip()
    return None


def parse_rating(html):
    """主商品评分。必须限定在主商品区块，否则会抓到推荐位的其他商品评分（导致抖动误报）"""
    # 优先：acrPopover 的 title（主商品专属）
    m = re.search(r'id="acrPopover"[^>]*title="([0-9.]+)\s+out of 5 stars"', html)
    if m:
        return float(m.group(1))
    # 兜底：averageCustomerReviews 区块内
    m = re.search(r'id="averageCustomerReviews".{0,800}?([0-9.]+)\s+out of 5 stars', html, re.S)
    if m:
        return float(m.group(1))
    return None


def parse_reviews(html):
    """主商品评论数。页面里 'N ratings' 大量出现且多为推荐位商品，不能直接取第一个"""
    # 优先：acrCustomerReviewText（主商品评论数，aria-label="3,432 Reviews"）
    m = re.search(r'id="acrCustomerReviewText"[^>]*aria-label="([0-9,]+)\s+Reviews?"', html)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.search(r'id="acrCustomerReviewText"[^>]*>\s*\(([0-9,]+)\)', html)
    if m:
        return int(m.group(1).replace(",", ""))
    # 次选：total-review-count（"3,432 global ratings"）
    m = re.search(r'data-hook="total-review-count"[^>]*>\s*([0-9,]+)\s+global ratings', html)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


SOURCE = "sellersprite_mcp_v2"
METRIC_SCHEMA = "asin.detail+keepa.trend:competitors:v1"
MCP_ENDPOINT = "https://mcp.sellersprite.com/v2/mcp"
DETAIL_FIELDS = ["asin", "marketplace", "title", "price", "currency", "categoryPath",
                 "bsr", "rating", "reviewCount", "recordUpdatedAt"]
METRIC_LABELS = {"price": "售价", "stock": "库存", "bsr": "大类 BSR", "sub_bsr": "小类 BSR",
                 "rating": "星级", "reviews": "评分数"}


def sellersprite_secret():
    secret = os.environ.get("SELLERSPRITE_MCP_SECRET_KEY", "").strip()
    if secret:
        return secret
    if os.environ.get("GITHUB_ACTIONS") != "true":
        try:
            config = json.loads((Path.home() / ".workbuddy" / "mcp.json").read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            config = {}
        except (OSError, ValueError):
            raise RuntimeError("无法读取 WorkBuddy MCP 配置") from None
        for server in config.get("mcpServers", {}).values():
            if not isinstance(server, dict) or server.get("url") != MCP_ENDPOINT or server.get("disabled") is True:
                continue
            for key, value in (server.get("headers") or {}).items():
                if key.lower() == "secret-key" and isinstance(value, str) and value.strip():
                    return value.strip()
    raise RuntimeError("未配置卖家精灵 MCP V2 密钥（SELLERSPRITE_MCP_SECRET_KEY）")


class NoMcpRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SellerSpriteMcp:
    def __init__(self, secret, timeout=30):
        self.secret, self.timeout = secret, timeout
        self.request_id = 0
        self.session = self.version = None
        self.opener = urllib.request.build_opener(NoMcpRedirect())

    def rpc(self, method, params=None, notification=False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notification:
            self.request_id += 1
            message["id"] = self.request_id
        headers = {"secret-key": self.secret, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.version:
            headers["Mcp-Protocol-Version"] = self.version
        request = urllib.request.Request(MCP_ENDPOINT, json.dumps(message).encode("utf-8"), headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                self.session = response.headers.get("Mcp-Session-Id") or self.session
                if notification:
                    return None
                if response.headers.get_content_type() == "text/event-stream":
                    result, lines = None, []
                    for raw in response:
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if line.startswith("data:"):
                            lines.append(line[5:].lstrip())
                        elif not line and lines:
                            event = json.loads("\n".join(lines))
                            lines.clear()
                            if isinstance(event, dict) and event.get("id") == message["id"]:
                                result = event
                                break
                else:
                    result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"卖家精灵 MCP {method} 返回 HTTP {exc.code}") from None
        except (OSError, ValueError):
            raise RuntimeError(f"卖家精灵 MCP {method} 连接、读取或解析失败") from None
        if not isinstance(result, dict) or result.get("id") != message["id"] or not isinstance(result.get("result"), dict):
            raise RuntimeError(f"卖家精灵 MCP {method} 未返回有效响应")
        return result["result"]

    def initialize(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                         "clientInfo": {"name": "amazon-competitor-monitor", "version": "1.0.0"}})
        self.version = result.get("protocolVersion")
        if not isinstance(self.version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.version):
            raise RuntimeError("卖家精灵 MCP 握手缺少有效协议版本")
        self.rpc("notifications/initialized", notification=True)

    def call(self, name, arguments, validate):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if not result.get("isError"):
            data = result.get("structuredContent")
            if isinstance(data, dict) and validate(data):
                return data
            for part in result.get("content") or []:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                try:
                    data = json.loads(part.get("text", ""))
                except (TypeError, ValueError):
                    continue
                if isinstance(data, dict) and validate(data):
                    return data
        raise RuntimeError(f"卖家精灵 {name} 未返回可用数据（请检查权限、额度或接口状态）")


def valid_number(value, minimum=0, maximum=float("inf")):
    return type(value) in (float, int) and minimum <= value < maximum


def valid_rank(value):
    return valid_number(value, 1) and value == int(value)


def detail_time(value):
    # 保留接口原始时区口径，不猜测无时区标记的时间。
    if isinstance(value, str):
        try:
            return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").isoformat(sep=" ")
        except ValueError:
            pass
    return None


def newest_subrank(trend, asin, now):
    if trend.get("asin") != asin or trend.get("dataAsin") != asin:
        return None, None
    ids = str(trend.get("nodeIdPath") or "").split(":")
    labels = str(trend.get("nodeLabelPath") or "").split(":")
    if len(ids) < 2 or not ids[-1].isdigit():
        return None, None
    ranks, times = trend["series"].get(f"subSalesRank:{ids[-1]}"), trend["ts"]
    if not isinstance(ranks, list) or len(ranks) != len(times):
        return None, None
    lo, hi = (now - datetime.timedelta(days=7)).timestamp(), now.timestamp()
    points = [(ts, val) for ts, val in zip(times, ranks)
              if type(ts) is int and lo <= ts <= hi and valid_rank(val)]
    if not points:
        return None, None
    ts, rank = max(points, key=lambda p: p[0])
    time_utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
    return {"rank": int(rank), "category": labels[-1], "node_id": ids[-1]}, time_utc


def seller_snapshots(config, now):
    market = config["marketplace"]
    asins = [item["asin"] for item in config["asins"]]
    client = SellerSpriteMcp(sellersprite_secret(), config.get("request", {}).get("timeout", 30))
    client.initialize()
    by_asin, detail_error = {}, None
    try:
        details = client.call("asin.detail", {"marketplace": market, "asins": asins, "includeFields": DETAIL_FIELDS},
                              lambda d: isinstance(d.get("items"), list))
        by_asin = {row.get("asin"): row for row in details["items"] if isinstance(row, dict)
                   and row.get("marketplace") == market and row.get("resultStatus") == "success"}
    except RuntimeError as exc:
        detail_error = str(exc)
    start = (now - datetime.timedelta(days=7)).date().isoformat()
    end = now.date().isoformat()
    for info in config["asins"]:
        asin = info["asin"]
        row = by_asin.get(asin, {})
        issues = [] if row else [detail_error or "商品详情未返回有效记录"]
        try:
            trend = client.call("keepa.trend", {"marketplace": market, "asin": asin,
                                "metrics": ["subSalesRank"], "dateRange": {"startDate": start, "endDate": end}},
                                lambda d: d.get("asin") == asin and isinstance(d.get("ts"), list)
                                and isinstance(d.get("series"), dict))
            sub, sub_time = newest_subrank(trend, asin, now)
            if sub is None:
                issues.append("近 7 天未返回该 ASIN 的有效小类 BSR")
        except RuntimeError as exc:
            sub, sub_time = None, None
            issues.append(str(exc))
        price, rating, reviews = row.get("price"), row.get("rating"), row.get("reviewCount")
        currency = row.get("currency")
        category_path = row.get("categoryPath")
        category = category_path[0] if isinstance(category_path, list) and category_path else None
        rank = row.get("bsr")
        stamp = detail_time(row.get("recordUpdatedAt"))
        data = {
            "source": SOURCE, "schema": METRIC_SCHEMA, "marketplace": market, "currency": currency,
            "asin": asin, "name": info["name"], "title": row.get("title"),
            "price": round(price, 2) if valid_number(price, 0.01) and currency == config["currency"] else None,
            "stock": None,  # MCP V2 的已验证接口未提供库存状态；禁止由价格、购物车或卖家数推断。
            "bsr": {"rank": int(rank), "category": category} if isinstance(category, str) and category and valid_rank(rank) else None,
            "sub_bsr": sub, "rating": round(rating, 1) if valid_number(rating, 0.1, 5.001) else None,
            "reviews": reviews if type(reviews) is int and reviews >= 0 else None,
            "source_times": {"price": stamp, "bsr": stamp, "sub_bsr": sub_time,
                             "rating": stamp, "reviews": stamp, "stock": None},
        }
        missing = [METRIC_LABELS[k] for k in ("price", "bsr", "rating", "reviews") if data[k] is None]
        if row and (missing or stamp is None):
            issues.append("商品详情缺少" + "、".join(missing + ([] if stamp else ["有效数据时间"])))
        yield info, data, issues


def metric_scope(data, key):
    if key == "price":
        return data.get("currency")
    if key == "bsr":
        return (data.get(key) or {}).get("category")
    if key == "sub_bsr":
        return (data.get(key) or {}).get("node_id")
    return key


def advance_baseline(current, previous):
    identity = ("source", "schema", "marketplace", "asin")
    matches = isinstance(previous, dict) and all(previous.get(k) == current[k] for k in identity)
    old = previous if matches else {}
    new_baseline = {**current, "source_times": dict(current["source_times"])}
    comparable, status = {}, {}
    for key in METRIC_LABELS:
        ts, last_ts = current["source_times"].get(key), (old.get("source_times") or {}).get(key)
        value, past = current.get(key), old.get(key)
        if value is None or not ts or (last_ts and ts <= last_ts):
            new_baseline[key] = past if last_ts else None
            new_baseline["source_times"][key] = last_ts
            if key == "price" and past is not None and last_ts:
                new_baseline["currency"] = old.get("currency")
            status[key] = ("未知（接口未提供）" if value is None else "无源时间" if not ts else
                           "源快照未更新" if ts == last_ts else "源快照早于基线")
        elif past is None or not last_ts:
            status[key] = "建立基线"
        elif metric_scope(current, key) != metric_scope(old, key):
            status[key] = "口径变更，重建基线"
        else:
            comparable[key] = past
            if key.endswith("bsr"):
                before, after = past["rank"], value["rank"]
                diff = before - after
                status[key] = (f"#{before:,} → #{after:,}（{'提升' if diff > 0 else '下滑'} {abs(diff):,} 位）"
                               if diff else "持平")
            elif key == "price":
                status[key] = f"${past:.2f} → ${value:.2f}（{value - past:+.2f} USD / {(value - past) / past:+.1%}）" if value != past else "持平"
            elif key == "rating":
                status[key] = f"{past:.1f} → {value:.1f} 星（{value - past:+.1f}）" if value != past else "持平"
            elif key == "stock":
                status[key] = f"{past} → {value}" if value != past else "持平"
            else:
                status[key] = f"{past:,} → {value:,}（{value - past:+,}）" if value != past else "持平"
    return new_baseline, comparable, status


def load_history():
    """优先从环境变量 HISTORY_STATE 读取（云端），否则读本地文件"""
    env_state = os.environ.get("HISTORY_STATE", "").strip()
    if env_state:
        try:
            return json.loads(env_state)
        except Exception as e:
            log(f"  解析 HISTORY_STATE 失败: {e}")
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_history(history):
    os.makedirs(DATA_DIR, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATA_DIR, delete=False) as f:
        temporary_path = f.name
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(temporary_path, HISTORY_PATH)
    # 云端无状态环境：输出快照供下次通过环境变量回传
    if os.environ.get("OUTPUT_STATE") == "1":
        print("\n===HISTORY_STATE_BEGIN===")
        print(json.dumps(history, ensure_ascii=False))
        print("===HISTORY_STATE_END===")


def append_alert(alert):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ALERTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(alert, ensure_ascii=False) + "\n")


def send_serverchan(alerts):
    """通过 Server酱 推送到微信（可用于 GitHub Actions 云端）"""
    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if not key:
        return None  # 未配置，交由其他方式
    try:
        title = f"亚马逊竞品预警 {len(alerts)} 条"
        desp_lines = []
        for a in alerts:
            desp_lines.append(f"**[{a['type']}]** {a['detail']}")
        desp = "\n\n".join(desp_lines)
        data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
        url = f"https://sctapi.ftqq.com/{key}.send"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        log(f"  Server酱 响应: {body[:200]}")
        if '"code":0' in body or '"errno":0' in body:
            log("  ✓ 微信推送成功（Server酱）")
            return True
        log("  ✗ 微信推送失败（Server酱返回异常）")
        return False
    except Exception as e:
        log(f"  ✗ Server酱 推送异常: {type(e).__name__}: {e}")
        return False


def _feishu_sign(secret, timestamp):
    """飞书自定义机器人签名校验：HMAC-SHA256(key=timestamp\\n secret, msg='')"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


ALERT_EMOJI = {
    "价格变化": "💰",
    "促销上线": "🏷️",
    "折扣变化": "🏷️",
    "促销结束": "🏁",
    "库存变化": "📦",
    "售罄": "🚨",
    "回货": "✅",
    "BSR排名变化": "📊",
    "小类BSR变化": "📈",
    "评分变化": "⭐",
    "评论激增": "💬",
    "抓取异常": "⚠️",
    "测试": "🧪",
}


def _fmt_snapshot(snapshot):
    """把本轮抓取数据格式化为卡片文本行"""
    lines = []
    for d in snapshot or []:
        big, sub = d.get("bsr") or {}, d.get("sub_bsr") or {}
        values = {
            "price": f"${d['price']:.2f}" if d.get("price") is not None else "—",
            "stock": d.get("stock") or "未知（卖家精灵未提供）",
            "bsr": f"{big['category']} #{big['rank']:,}" if big else "—",
            "sub_bsr": f"{sub['category']} #{sub['rank']:,}" if sub else "—",
            "rating": f"{d['rating']:.1f} 星" if d.get("rating") is not None else "—",
            "reviews": f"{d['reviews']:,}" if d.get("reviews") is not None else "—",
        }
        statuses = d.get("status") or {}
        summary = [f"{METRIC_LABELS[k]}：{value}" + (f"（{statuses[k]}）" if k in statuses and k != "stock" else "")
                   for k, value in values.items()]
        stamps = d.get("source_times") or {}
        lines.append(f"**{d['name']}** ({d['asin']}) · 卖家精灵 MCP V2\n" + " · ".join(summary)
                     + f"\n详情时间（接口原值）：{stamps.get('price') or '—'} / 小类 BSR 时间（UTC）：{stamps.get('sub_bsr') or '—'}")
    return lines


def send_feishu(alerts, snapshot=None):
    """飞书自定义机器人推送（卡片消息）。支持签名校验。未配置返回 None。
    alerts 为空时也推送（巡检正常报告），snapshot 为本轮抓取的数据快照。
    """
    url = (os.environ.get("FEISHU_WEBHOOK", "") or "").strip()
    if not url:
        return None

    secret = (os.environ.get("FEISHU_SECRET", "") or "").strip()

    try:
        is_test = bool(alerts) and all(a.get("type") == "测试" for a in alerts)
        if is_test:
            title = "🧪 测试通知"
            template = "blue"
        elif alerts:
            title = f"⚠️ 竞品预警 {len(alerts)} 条"
            serious = any(
                k in a.get("type", "") for a in alerts
                for k in ("售罄", "回货", "取数异常")
            )
            template = "red" if serious else "orange"
        elif any((d.get("status") or {}).get("price") == "建立基线" for d in snapshot or []):
            title = "竞品巡检 · 建立卖家精灵基线"
            template = "blue"
        else:
            title = "竞品巡检 · 可比较指标未触发预警"
            template = "green"

        elements = []
        # 1) 异常明细放最前（用户要求：有变化要重点提醒）
        if alerts:
            for i, a in enumerate(alerts):
                etype = a.get("type", "预警")
                emoji = ALERT_EMOJI.get(etype, "🔔")
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"{emoji} **{etype}**\n{a.get('detail', '')}",
                    },
                })
                if i < len(alerts) - 1:
                    elements.append({"tag": "hr"})

        # 2) 本轮数据快照
        snap_lines = _fmt_snapshot(snapshot)
        if snap_lines:
            if alerts:
                elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "📊 **本轮数据**"},
            })
            for line in snap_lines:
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": line},
                })

        now_str = bjnow("%Y-%m-%d %H:%M")
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看 / 手动触发抓取"},
                    "type": "primary",
                    "url": "https://github.com/Guo-collar/amazon-competitor-monitor/actions/workflows/monitor.yml",
                }
            ],
        })
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"巡检时间 {now_str} · 每{SCHEDULE_INTERVAL_HOURS}小时自动巡检 · 亚马逊竞品监控"}],
        })

        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": template,
                },
                "elements": elements,
            },
        }

        # 可选签名校验
        post_url = url
        if secret:
            ts = str(int(time.time()))
            sign = _feishu_sign(secret, ts)
            sep = "&" if "?" in url else "?"
            post_url = f"{url}{sep}timestamp={ts}&sign={urllib.parse.quote(sign)}"

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(post_url, data=data, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        # 只记录结果，不把第三方响应体或消息内容写入公开的 Actions 日志。
        try:
            rj = json.loads(body)
        except ValueError:
            rj = {}
        code = rj.get("code", rj.get("StatusCode")) if isinstance(rj, dict) else None
        if type(code) is int and code == 0:
            log("飞书推送成功")
            return True
        log(f"飞书推送失败（返回码：{code if type(code) is int else '无有效返回码'}）")
        return False
    except Exception as e:
        log(f"飞书推送异常：{type(e).__name__}")
        return False


def send_webhook(alerts):
    """通用群机器人 Webhook（企业微信/钉钉/其他）。未配置返回 None。"""
    url = os.environ.get("WEBHOOK_URL", "").strip()
    if not url:
        return None
    try:
        title = f"亚马逊竞品预警 {len(alerts)} 条"
        lines = [f"### {title}", ""]
        for a in alerts:
            lines.append(f"**[{a['type']}]** {a['detail']}")
        content = "\n".join(lines)
        if "dingtalk" in url:
            payload = {"msgtype": "markdown", "markdown": {"title": title, "text": content}}
        else:
            payload = {"msgtype": "markdown", "markdown": {"content": content}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        log(f"  Webhook 响应: {body[:200]}")
        try:
            rj = json.loads(body)
        except Exception:
            rj = {}
        code = rj.get("errcode", rj.get("code", rj.get("StatusCode")))
        if code == 0:
            log("  ✓ 群机器人推送成功")
            return True
        log(f"  ✗ 群机器人推送失败（code={code}）")
        return False
    except Exception as e:
        log(f"  ✗ Webhook 推送异常: {type(e).__name__}: {e}")
        return False


def send_notification(alerts, config, snapshot=None):
    """统一推送入口：飞书 → Server酱 → 通用Webhook → 邮件，按优先级尝试。
    alerts 为空表示本轮无异常（巡检正常报告也需要推送）。
    """
    log("  --- 推送配置检查 ---")
    has_feishu = bool((os.environ.get("FEISHU_WEBHOOK", "") or "").strip())
    has_serverchan = bool(os.environ.get("SERVERCHAN_KEY", "").strip())
    has_webhook = bool(os.environ.get("WEBHOOK_URL", "").strip())
    has_smtp = bool(os.environ.get("SMTP_HOST", "").strip() and os.environ.get("SMTP_USER", "").strip())
    log(f"    FEISHU_WEBHOOK = {'已设置' if has_feishu else '(空)'}")
    log(f"    FEISHU_SECRET  = {'已设置' if (os.environ.get('FEISHU_SECRET', '') or '').strip() else '(空，无签名)'}")
    log(f"    SERVERCHAN_KEY = {'已设置' if has_serverchan else '(空)'}")
    log(f"    WEBHOOK_URL    = {'已设置' if has_webhook else '(空)'}")
    log(f"    邮件 SMTP      = {'已设置' if has_smtp else '(空)'}")

    # 非飞书渠道不支持快照卡片，无异常时补一条占位说明
    plain_alerts = alerts or [{"type": "巡检报告", "detail": "可比较指标未触发预警。\n" + "\n".join(_fmt_snapshot(snapshot))}]

    if has_feishu:
        r = send_feishu(alerts, snapshot)
        if r:
            return True
        log("  飞书推送失败，尝试其他方式...")
    if has_serverchan:
        r = send_serverchan(plain_alerts)
        if r:
            return True
        log("  Server酱 失败，尝试其他方式...")
    if has_webhook:
        r = send_webhook(plain_alerts)
        if r:
            return True
        log("  Webhook 失败，尝试其他方式...")
    if has_smtp:
        return send_email(plain_alerts, config)

    log("  ✗ 未配置任何推送方式（FEISHU_WEBHOOK / SERVERCHAN_KEY / WEBHOOK_URL / SMTP）")
    return False


def send_email(alerts, config):
    """通过 SMTP 发送预警邮件。未配置则跳过。"""
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    alert_to = os.environ.get("ALERT_TO", "").strip()
    smtp_port_str = os.environ.get("SMTP_PORT", "465").strip()

    log("  --- SMTP 配置检查 ---")
    log(f"    SMTP_HOST = {smtp_host if smtp_host else '(空)'}")
    log(f"    SMTP_PORT = {smtp_port_str if smtp_port_str else '(空)'}")
    log(f"    SMTP_USER = {smtp_user if smtp_user else '(空)'}")
    log(f"    SMTP_PASS = {'已设置(长度%d)' % len(smtp_pass) if smtp_pass else '(空)'}")
    log(f"    ALERT_TO  = {alert_to if alert_to else '(空)'}")

    if not (smtp_host and smtp_user and smtp_pass and alert_to):
        log("  ✗ 邮件未发送：上述配置存在缺失，请检查 GitHub Secrets")
        return False

    try:
        smtp_port = int(smtp_port_str or "465")
        lines = ["亚马逊竞品监控预警", ""]
        for a in alerts:
            lines.append(f"[{a['type']}] {a['detail']}")
        body = "\n".join(lines)
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(f"亚马逊竞品预警 {len(alerts)} 条", "utf-8")
        msg["From"] = smtp_user
        msg["To"] = alert_to

        log(f"  连接服务器 {smtp_host}:{smtp_port} ...")
        use_ssl = smtp_port == 465
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            server.starttls()
        log("  连接成功，正在登录 ...")
        server.login(smtp_user, smtp_pass)
        log("  登录成功，正在发送 ...")
        server.sendmail(smtp_user, [alert_to], msg.as_string())
        server.quit()
        log(f"  ✓ 预警邮件已发送至 {alert_to}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        log(f"  ✗ 登录认证失败（授权码可能错误或已失效）: {e}")
        return False
    except smtplib.SMTPException as e:
        log(f"  ✗ SMTP 协议错误: {type(e).__name__}: {e}")
        return False
    except Exception as e:
        log(f"  ✗ 邮件发送失败: {type(e).__name__}: {e}")
        return False


def _rank_of(record, field):
    """安全读取 bsr / sub_bsr 的 rank。
    字段可能缺失，也可能存在但值为 None（页面没抓到 BSR 时），两者都必须安全返回 None。
    历史 bug：用 record.get(field, {}).get("rank") 时，字段值为 None 会抛
    'NoneType' object has no attribute 'get'，导致整轮被判为「抓取异常」。
    """
    v = (record or {}).get(field)
    return v.get("rank") if isinstance(v, dict) else None


def compare_and_alert(asin_info, old, new):
    asin = asin_info["asin"]
    name = asin_info["name"]
    alerts = []
    rules = load_config()["alert_rules"]

    old_price = old.get("price") if old else None
    new_price = new.get("price")
    if old_price is not None and new_price is not None and old_price != new_price:
        diff = round(new_price - old_price, 2)
        pct = diff / old_price * 100
        if abs(diff) >= rules["price_change_threshold_usd"] or abs(pct) >= rules["price_change_threshold_pct"]:
            direction = "上涨" if diff > 0 else "下降"
            alerts.append({"type": "价格变化", "detail": f"{name}({asin}) 价格{direction}: ${old_price:.2f} → ${new_price:.2f} (变动 {diff:+.2f} USD / {pct:+.1f}%)"})

    old_stock = old.get("stock") if old else None
    new_stock = new.get("stock")
    if old_stock and new_stock and rules["stock_change"] and old_stock != new_stock:
        alerts.append({"type": "库存变化", "detail": f"{name}({asin}) 库存变化: '{old_stock}' → '{new_stock}'"})
    if rules["out_of_stock"] and old_stock and old_stock != "Out of Stock" and new_stock == "Out of Stock":
        alerts.append({"type": "售罄预警", "detail": f"{name}({asin}) 已售罄 (Out of Stock)!"})
    if rules["back_in_stock"] and old_stock == "Out of Stock" and new_stock and new_stock != "Out of Stock":
        alerts.append({"type": "回货预警", "detail": f"{name}({asin}) 已回货: {new_stock}"})

    old_bsr = _rank_of(old, "bsr")
    new_bsr = _rank_of(new, "bsr")
    if old_bsr and new_bsr:
        change_pct = (old_bsr - new_bsr) / old_bsr * 100
        if abs(change_pct) >= rules["bsr_change_threshold"]:
            direction = "上升(变好)" if change_pct > 0 else "下降(变差)"
            alerts.append({"type": "大类BSR变化", "detail": f"{name}({asin}) 大类BSR排名{direction}: #{old_bsr} → #{new_bsr} ({change_pct:+.1f}%)"})

    old_sub_bsr = _rank_of(old, "sub_bsr")
    new_sub_bsr = _rank_of(new, "sub_bsr")
    if old_sub_bsr and new_sub_bsr:
        change_pct = (old_sub_bsr - new_sub_bsr) / old_sub_bsr * 100
        rank_diff = abs(new_sub_bsr - old_sub_bsr)
        thr_pct = rules.get("sub_bsr_change_threshold_pct", 15)
        thr_rank = rules.get("sub_bsr_change_threshold_rank", 3)
        if abs(change_pct) >= thr_pct or rank_diff >= thr_rank:
            direction = "上升(变好)" if change_pct > 0 else "下降(变差)"
            alerts.append({"type": "小类BSR变化", "detail": f"{name}({asin}) 小类BSR排名{direction}: #{old_sub_bsr} → #{new_sub_bsr} (变动 {change_pct:+.1f}%, {rank_diff}名)"})

    old_rating = old.get("rating") if old else None
    new_rating = new.get("rating")
    if old_rating is not None and new_rating is not None and abs(round(new_rating - old_rating, 1)) >= 0.1:
        direction = "上升" if new_rating > old_rating else "下降"
        alerts.append({"type": "评分变化", "detail": f"{name}({asin}) 评分{direction}: {old_rating} → {new_rating} 星"})

    old_reviews = old.get("reviews") if old else None
    new_reviews = new.get("reviews")
    if old_reviews and new_reviews and old_reviews > 0:
        growth_pct = (new_reviews - old_reviews) / old_reviews * 100
        if abs(growth_pct) > 150:
            alerts.append({"type": "评分数口径待核查", "detail": f"{name}({asin}) 卖家精灵评分数变化: {old_reviews:,} → {new_reviews:,}（{growth_pct:+.1f}%），不等同于实际新增或删除评价。"})
        elif growth_pct >= 20:
            alerts.append({"type": "评分数激增", "detail": f"{name}({asin}) 评分数快速增长: {old_reviews:,} → {new_reviews:,} ({growth_pct:+.1f}%)"})

    return alerts


def main():
    init_mode = "--init" in sys.argv
    dry_run = DRY_RUN
    scheduled_mode = "--scheduled" in sys.argv or os.environ.get("SCHEDULED_MODE") == "1"
    test_email_mode = "--test-email" in sys.argv or os.environ.get("TEST_EMAIL") == "1"
    if not dry_run:
        os.makedirs(DATA_DIR, exist_ok=True)

    if scheduled_mode and not dry_run:
        should_run, reason = schedule_gate()
        if not should_run:
            log(f"⏭ 定时门控：{reason}")
            return
        log(f"✅ 定时门控：{reason}")

    if not dry_run:
        Path(DATA_DIR, ".ran").write_text(bjnow(), encoding="utf-8")

    config = load_config()
    log(f"===== 卖家精灵 MCP V2 · {'初始化基线' if init_mode else '竞品巡检'} =====")

    # 仅测试推送：不抓取，直接发一条测试通知
    if test_email_mode and not init_mode and not dry_run and os.environ.get("TEST_EMAIL_ONLY") == "1":
        ok = send_notification([{"type": "测试", "detail": "仅测试推送模式，未执行抓取。收到即代表配置成功。"}], config)
        log(f"测试通知{'发送成功' if ok else '发送失败'}")
        return

    history = load_history()
    now = datetime.datetime.now(datetime.timezone.utc)
    all_alerts, snapshot = [], []
    updates = {}
    try:
        for info, data, issues in seller_snapshots(config, now):
            asin = info["asin"]
            snapshot.append(data)
            baseline, comparable, status = advance_baseline(data, history.get(asin))
            data["status"] = status
            if any(baseline["source_times"].values()):
                updates[asin] = baseline
            log("\n".join(_fmt_snapshot([data])))
            if issues:
                all_alerts.append({"type": "取数异常", "detail": f"{info['name']} ({asin})：{'；'.join(issues)}"})
            if not init_mode:
                valid_current = {key: data[key] for key in comparable}
                all_alerts.extend(compare_and_alert(info, comparable, valid_current))
    except RuntimeError as exc:
        all_alerts.append({"type": "取数异常", "detail": str(exc)})
    for alert in all_alerts:
        log(f"预警 [{alert['type']}] {alert['detail']}")
        if not dry_run:
            append_alert({"time": bjnow(), "source": SOURCE, **alert})
    if dry_run:
        return 0 if any(any(d["source_times"].values()) for d in snapshot) and not any(a["type"] == "取数异常" for a in all_alerts) else 1
    if updates:
        # 本轮只更新有有效数据时间的同源记录；旧网页历史另存，绝不用于比较。
        save_history({**history, **updates})
    notified = init_mode or send_notification(all_alerts, config, snapshot)
    log(f"===== 监控结束：{len(all_alerts)} 条预警 =====")
    mark_run()
    if os.environ.get("GITHUB_ACTIONS") == "true" and not notified:
        log("云端通知发送失败；保留本轮有效基线，请检查通知配置。")
        return 1
    return 0 if updates and not any(a["type"] == "取数异常" for a in all_alerts) else 1


if __name__ == "__main__":
    sys.exit(main())

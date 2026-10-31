#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
亚马逊竞争对手监控脚本（云端部署版）
监控多个 ASIN 的价格、库存、BSR（大类+小类）、评分、评论数变化，异常时预警。

用法:
  python monitor.py            # 常规监控：抓取+对比+预警
  python monitor.py --init     # 首次运行：仅建立基线不预警

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
from email.mime.text import MIMEText
from email.header import Header

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
ALERTS_PATH = os.path.join(DATA_DIR, "alerts.jsonl")
LOG_PATH = os.path.join(DATA_DIR, "monitor.log")

# 统一使用北京时间（GitHub Actions runner 默认是 UTC，会造成 8 小时时差）
TZ_BEIJING = datetime.timezone(datetime.timedelta(hours=8))


def bjnow(fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.datetime.now(TZ_BEIJING).strftime(fmt)


def log(msg):
    line = f"[{bjnow()}] {msg}"
    print(line)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_page(asin, config):
    """用 curl 抓取页面（curl 的 TLS 特征能绕过亚马逊机器人检测）"""
    url = f"https://www.amazon.com/dp/{asin}?currencyOfPreference=USD&language=en_US"
    timeout = config["request"]["timeout"]
    retry = config["request"]["retry"]
    last_err = None
    for i in range(retry):
        tmp = os.path.join(tempfile.gettempdir(), f"amz_{asin}_{os.getpid()}.html")
        try:
            cmd = [
                "curl", "-sL",
                "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "-H", "Accept-Language: en-US,en;q=0.9",
                "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "-b", "i18n-prefs=USD; lc-main=en_US;",
                "--max-time", str(timeout),
                "-o", tmp,
                url,
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=timeout + 10)
            with open(tmp, "r", encoding="utf-8", errors="ignore") as f:
                html = f.read()
            if os.path.exists(tmp):
                os.remove(tmp)
            if "productTitle" in html or "a-offscreen" in html or len(html) > 100000:
                return html
            log(f"  [{asin}] 第{i+1}次返回疑似验证页(长度{len(html)}), 重试...")
            last_err = f"疑似验证页, 长度{len(html)}"
        except Exception as e:
            last_err = e
            log(f"  [{asin}] 第{i+1}次抓取失败: {e}")
        time.sleep(4)
    raise RuntimeError(f"抓取失败: {last_err}")


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
    """大类 Best Seller Rank（zg_ts）"""
    m = re.search(r'zg_ts">[^#]*#([0-9,]+)\s+in\s+<a[^>]*>([^<]+)', html)
    if m:
        return {"rank": int(m.group(1).replace(",", "")), "category": m.group(2).strip()}
    m = re.search(r'#([0-9,]+)\s+in\s+([^<(]+)', html)
    if m:
        return {"rank": int(m.group(1).replace(",", "")), "category": m.group(2).strip()}
    return None


def parse_sub_bsr(html):
    """小类 Best Seller Rank（zg_hrsr）"""
    m = re.search(r'zg_hrsr">\s*<li><span class="a-list-item">\s*#([0-9,]+)\s+in\s+<a[^>]*>([^<]+)', html)
    if m:
        return {"rank": int(m.group(1).replace(",", "")), "category": m.group(2).strip()}
    return None


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
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
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
    "测试": "🧪",
}


def send_feishu(alerts):
    """飞书自定义机器人推送（卡片消息）。支持签名校验。未配置返回 None。"""
    url = (os.environ.get("FEISHU_WEBHOOK", "") or "").strip()
    if not url:
        return None

    secret = (os.environ.get("FEISHU_SECRET", "") or "").strip()

    try:
        title = f"亚马逊竞品预警 {len(alerts)} 条"
        # 有任何售罄/回货用红色，其余用橙色
        serious = any("售罄" in a.get("type", "") or "回货" in a.get("type", "") for a in alerts)
        template = "red" if serious else "orange"

        elements = []
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
            "elements": [{"tag": "plain_text", "content": f"监控时间 {now_str} · 亚马逊竞品监控"}],
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
        log(f"  飞书响应: {body[:300]}")

        # 严格判断返回码（飞书报错时也是非空 JSON，不能只看非空）
        try:
            rj = json.loads(body)
        except Exception:
            rj = {}
        code = rj.get("code", rj.get("StatusCode"))
        if code == 0:
            log("  ✓ 飞书推送成功")
            return True
        log(f"  ✗ 飞书推送失败（code={code}, msg={rj.get('msg') or rj.get('StatusMessage')}）")
        return False
    except Exception as e:
        log(f"  ✗ 飞书推送异常: {type(e).__name__}: {e}")
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


def send_notification(alerts, config):
    """统一推送入口：飞书 → Server酱 → 通用Webhook → 邮件，按优先级尝试"""
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

    if has_feishu:
        r = send_feishu(alerts)
        if r:
            return True
        log("  飞书推送失败，尝试其他方式...")
    if has_serverchan:
        r = send_serverchan(alerts)
        if r:
            return True
        log("  Server酱 失败，尝试其他方式...")
    if has_webhook:
        r = send_webhook(alerts)
        if r:
            return True
        log("  Webhook 失败，尝试其他方式...")
    if has_smtp:
        return send_email(alerts, config)

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


def compare_and_alert(asin_info, old, new):
    asin = asin_info["asin"]
    name = asin_info["name"]
    alerts = []
    rules = load_config()["alert_rules"]

    old_price = old.get("price") if old else None
    new_price = new.get("price")
    if old_price is not None and new_price is not None and old_price != new_price:
        diff = new_price - old_price
        pct = diff / old_price * 100
        if abs(diff) >= rules["price_change_threshold_usd"] or abs(pct) >= rules["price_change_threshold_pct"]:
            direction = "上涨" if diff > 0 else "下降"
            alerts.append({"type": "价格变化", "detail": f"{name}({asin}) 价格{direction}: ${old_price:.2f} → ${new_price:.2f} (变动 {diff:+.2f} USD / {pct:+.1f}%)"})

    old_stock = old.get("stock") if old else None
    new_stock = new.get("stock")
    if rules["stock_change"] and old_stock and old_stock != new_stock:
        alerts.append({"type": "库存变化", "detail": f"{name}({asin}) 库存变化: '{old_stock}' → '{new_stock}'"})
    if rules["out_of_stock"] and old_stock != "Out of Stock" and new_stock == "Out of Stock":
        alerts.append({"type": "售罄预警", "detail": f"{name}({asin}) 已售罄 (Out of Stock)!"})
    if rules["back_in_stock"] and old_stock == "Out of Stock" and new_stock and new_stock != "Out of Stock":
        alerts.append({"type": "回货预警", "detail": f"{name}({asin}) 已回货: {new_stock}"})

    old_bsr = old.get("bsr", {}).get("rank") if old else None
    new_bsr = new.get("bsr", {}).get("rank") if new else None
    if old_bsr and new_bsr:
        change_pct = (old_bsr - new_bsr) / old_bsr * 100
        if abs(change_pct) >= rules["bsr_change_threshold"]:
            direction = "上升(变好)" if change_pct > 0 else "下降(变差)"
            alerts.append({"type": "大类BSR变化", "detail": f"{name}({asin}) 大类BSR排名{direction}: #{old_bsr} → #{new_bsr} ({change_pct:+.1f}%)"})

    old_sub_bsr = old.get("sub_bsr", {}).get("rank") if old else None
    new_sub_bsr = new.get("sub_bsr", {}).get("rank") if new else None
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
    if old_rating is not None and new_rating is not None and abs(new_rating - old_rating) >= 0.1:
        direction = "上升" if new_rating > old_rating else "下降"
        alerts.append({"type": "评分变化", "detail": f"{name}({asin}) 评分{direction}: {old_rating} → {new_rating} 星"})

    old_reviews = old.get("reviews") if old else None
    new_reviews = new.get("reviews")
    if old_reviews and new_reviews and old_reviews > 0:
        growth_pct = (new_reviews - old_reviews) / old_reviews * 100
        # 上限保护：单轮暴涨 >150% 基本是页面抓到别的变体，不预警
        if growth_pct >= 20 and growth_pct <= 150:
            alerts.append({"type": "评论激增", "detail": f"{name}({asin}) 评论数快速增长: {old_reviews} → {new_reviews} ({growth_pct:+.1f}%)"})

    old_discount = old.get("discount") if old else None
    new_discount = new.get("discount")
    # 折扣是派生值，只有新旧的划线原价都可信时才比较，否则容易因解析漂移误报
    price_ok = new.get("list_price") is not None and (old.get("list_price") is not None if old else True)
    if price_ok and new_discount is not None and old_discount != new_discount:
        if old_discount is None:
            alerts.append({"type": "促销上线", "detail": f"{name}({asin}) 新增折扣: {new_discount}% off (现价 ${new.get('price')}, 原价 ${new.get('list_price')})"})
        else:
            direction = "加大" if new_discount > old_discount else "减小"
            alerts.append({"type": "折扣变化", "detail": f"{name}({asin}) 折扣{direction}: {old_discount}% → {new_discount}% off"})
    elif price_ok and new_discount is None and old_discount is not None:
        alerts.append({"type": "促销结束", "detail": f"{name}({asin}) 折扣已取消 (原 {old_discount}% off)"})

    return alerts


def main():
    init_mode = "--init" in sys.argv
    test_email_mode = "--test-email" in sys.argv or os.environ.get("TEST_EMAIL") == "1"
    config = load_config()
    os.makedirs(DATA_DIR, exist_ok=True)
    log(f"===== 监控开始 ({'初始化基线' if init_mode else '常规监控'}{' + 测试邮件' if test_email_mode else ''}) =====")

    # 仅测试推送：不抓取，直接发一条测试通知
    if test_email_mode and "--init" not in sys.argv and os.environ.get("TEST_EMAIL_ONLY") == "1":
        ok = send_notification([{"type": "测试", "detail": "仅测试推送模式，未执行抓取。收到即代表配置成功。"}], config)
        log(f"测试通知{'发送成功' if ok else '发送失败'}")
        return

    history = load_history()
    now = bjnow()
    all_alerts = []

    for asin_info in config["asins"]:
        asin = asin_info["asin"]
        name = asin_info["name"]
        log(f"抓取 {name} ({asin}) ...")
        try:
            html = fetch_page(asin, config)
            cur_price = parse_price(html)
            lst_price = parse_list_price(html, cur_price)
            # 折扣优先按价格自算（页面上的 "N% off" 可能来自其他商品）
            if cur_price and lst_price and lst_price > cur_price:
                calc_discount = round((lst_price - cur_price) / lst_price * 100)
            else:
                calc_discount = parse_discount(html)

            new_data = {
                "asin": asin,
                "name": name,
                "title": parse_title(html),
                "price": cur_price,
                "list_price": lst_price,
                "discount": calc_discount,
                "stock": parse_stock(html),
                "bsr": parse_bsr(html),
                "sub_bsr": parse_sub_bsr(html),
                "rating": parse_rating(html),
                "reviews": parse_reviews(html),
                "time": now,
            }
            # 评论数只增不减：若比基线变小，视为页面解析抖动，保留旧值不覆盖
            old_data = history.get(asin)
            if old_data and new_data["reviews"] and old_data.get("reviews"):
                if new_data["reviews"] < old_data["reviews"]:
                    log(f"  ⚠ 评论数异常回退 {old_data['reviews']} → {new_data['reviews']}，判定为页面抖动，沿用旧值")
                    new_data["reviews"] = old_data["reviews"]
            log(f"  → 价格: {new_data['price']} | 原价: {new_data['list_price']} | 折扣: {new_data['discount']}% | 库存: {new_data['stock']} | 大类BSR: {new_data['bsr']} | 小类BSR: {new_data['sub_bsr']} | 评分: {new_data['rating']} | 评论数: {new_data['reviews']}")
            log(f"  → 标题: {new_data['title']}")

            if not init_mode and old_data:
                alerts = compare_and_alert(asin_info, old_data, new_data)
                all_alerts.extend(alerts)
                for a in alerts:
                    log(f"  ⚠ 预警: [{a['type']}] {a['detail']}")

            history[asin] = new_data
            time.sleep(config["request"].get("delay_between_asins", 5))
        except Exception as e:
            log(f"  ✗ {name} ({asin}) 抓取异常: {e}")

    save_history(history)

    if all_alerts:
        for a in all_alerts:
            append_alert({"time": now, **a})
        log(f"===== 本轮共 {len(all_alerts)} 条预警 =====")
        send_notification(all_alerts, config)
    else:
        log("===== 本轮无异常 =====")
        if test_email_mode:
            log("  测试模式：发送测试通知验证推送链路")
            send_notification([{"type": "测试", "detail": "这是一条测试通知，用于验证预警推送链路是否正常。收到即代表配置成功。"}], config)
    log("===== 监控结束 =====\n")


if __name__ == "__main__":
    main()

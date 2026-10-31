#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
亚马逊竞争对手监控脚本（云端部署版）
监控多个 ASIN 的价格、库存、BSR（大类+小类）、评分、评论数变化，异常时预警。

用法:
  python monitor.py            # 常规监控：抓取+对比+预警
  python monitor.py --init     # 首次运行：仅建立基线不预警

邮件推送（可选）：
  通过环境变量配置 SMTP，有异常时发送邮件。未配置则不发送（仅写日志/预警文件）。
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / ALERT_TO

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
import datetime
import subprocess
import tempfile
import smtplib
from email.mime.text import MIMEText
from email.header import Header

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
ALERTS_PATH = os.path.join(DATA_DIR, "alerts.jsonl")
LOG_PATH = os.path.join(DATA_DIR, "monitor.log")


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
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


def parse_list_price(html):
    m = re.findall(r'data-a-strike="true"[^>]*>\s*<span class="a-offscreen">\s*\$([0-9]+(?:\.[0-9]{1,2})?)', html)
    if m:
        return float(m[0])
    return None


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
    m = re.search(r'([0-9.]+)\s+out of 5 stars', html)
    if m:
        return float(m.group(1))
    return None


def parse_reviews(html):
    m = re.search(r'([0-9,]+)\s+ratings', html)
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


def send_email(alerts, config):
    """通过 SMTP 发送预警邮件。未配置则跳过。"""
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    alert_to = os.environ.get("ALERT_TO", "")
    if not (smtp_host and smtp_user and smtp_pass and alert_to):
        log("  未配置 SMTP 环境变量，跳过邮件发送")
        return

    try:
        smtp_port = int(os.environ.get("SMTP_PORT", "465"))
        lines = ["亚马逊竞品监控预警", ""]
        for a in alerts:
            lines.append(f"[{a['type']}] {a['detail']}")
        body = "\n".join(lines)
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(f"亚马逊竞品预警 {len(alerts)} 条", "utf-8")
        msg["From"] = smtp_user
        msg["To"] = alert_to

        use_ssl = smtp_port == 465
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [alert_to], msg.as_string())
        server.quit()
        log(f"  预警邮件已发送至 {alert_to}")
    except Exception as e:
        log(f"  邮件发送失败: {e}")


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
        if growth_pct >= 20:
            alerts.append({"type": "评论激增", "detail": f"{name}({asin}) 评论数快速增长: {old_reviews} → {new_reviews} ({growth_pct:+.1f}%)"})

    old_discount = old.get("discount") if old else None
    new_discount = new.get("discount")
    if new_discount is not None and old_discount != new_discount:
        if old_discount is None:
            alerts.append({"type": "促销上线", "detail": f"{name}({asin}) 新增折扣: {new_discount}% off (现价 ${new.get('price')}, 原价 ${new.get('list_price')})"})
        else:
            direction = "加大" if new_discount > old_discount else "减小"
            alerts.append({"type": "折扣变化", "detail": f"{name}({asin}) 折扣{direction}: {old_discount}% → {new_discount}% off"})
    elif new_discount is None and old_discount is not None:
        alerts.append({"type": "促销结束", "detail": f"{name}({asin}) 折扣已取消 (原 {old_discount}% off)"})

    return alerts


def main():
    init_mode = "--init" in sys.argv
    config = load_config()
    os.makedirs(DATA_DIR, exist_ok=True)
    log(f"===== 监控开始 ({'初始化基线' if init_mode else '常规监控'}) =====")

    history = load_history()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_alerts = []

    for asin_info in config["asins"]:
        asin = asin_info["asin"]
        name = asin_info["name"]
        log(f"抓取 {name} ({asin}) ...")
        try:
            html = fetch_page(asin, config)
            new_data = {
                "asin": asin,
                "name": name,
                "title": parse_title(html),
                "price": parse_price(html),
                "list_price": parse_list_price(html),
                "discount": parse_discount(html),
                "stock": parse_stock(html),
                "bsr": parse_bsr(html),
                "sub_bsr": parse_sub_bsr(html),
                "rating": parse_rating(html),
                "reviews": parse_reviews(html),
                "time": now,
            }
            log(f"  → 价格: {new_data['price']} | 原价: {new_data['list_price']} | 折扣: {new_data['discount']}% | 库存: {new_data['stock']} | 大类BSR: {new_data['bsr']} | 小类BSR: {new_data['sub_bsr']} | 评分: {new_data['rating']} | 评论数: {new_data['reviews']}")
            log(f"  → 标题: {new_data['title']}")

            old_data = history.get(asin)
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
        send_email(all_alerts, config)
    else:
        log("===== 本轮无异常 =====")
    log("===== 监控结束 =====\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

WINDOW_MINUTES = 12
IMPACTS = {"High"}
CURRENCIES = {"USD", "AUD", "CHF", "GBP", "NZD", "EUR", "CAD", "JPY"}

CALENDAR_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]
CALENDAR_LINK = "https://www.forexfactory.com/calendar"

NEWS_FEEDS = {
    "FXStreet": "https://www.fxstreet.com/rss/news",
    "ForexLive": "https://www.forexlive.com/feed/news",
    "Investing.com": "https://www.investing.com/rss/news_1.rss",
    "ActionForex": "https://www.actionforex.com/feed/",
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "CNBC-Economy": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
}

KEYWORDS = [
    "fomc", "fed ", "federal reserve", "powell", "rate decision", "rate cut", "rate hike",
    "interest rate", "nfp", "non-farm", "nonfarm", "payrolls", "cpi", "inflation", "pce",
    "gdp", "unemployment", "jobless", "ecb", "lagarde", "boe", "bailey", "boj", "ueda",
    "snb", "rba", "rbnz", "boc", "tariff", "sanction", "war", "attack", "missile", "iran",
    "israel", "russia", "ukraine", "china", "trump", "treasury", "gold", "intervention",
    "emergency", "breaking", "flash", "default", "shutdown", "opec",
]
KEYWORD_RE = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
TEHRAN = timezone(timedelta(hours=3, minutes=30))
UA = "Mozilla/5.0 (compatible; NewsAlarmBot/1.0)"


def fetch(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("[بدون تلگرام] " + text)
        return True
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "disable_web_page_preview": "false",
        }).encode()
        fetch("https://api.telegram.org/bot%s/sendMessage" % token, data=data)
        return True
    except Exception as e:
        print("خطا در ارسال تلگرام: %s" % e)
        return False


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
            s.setdefault("events", [])
            s.setdefault("news", [])
            s.setdefault("seeded", False)
            return s
    except Exception:
        return {"events": [], "news": [], "seeded": False}


def save_state(s):
    s["events"] = s["events"][-1500:]
    s["news"] = s["news"][-4000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f)


INVERSE = ("unemployment", "jobless", "claims")
RISK_UP = ("war", "attack", "missile", "iran", "israel", "russia", "ukraine", "emergency")
RATE_DOWN = ("rate cut", "dovish", "ceasefire")
RATE_UP = ("rate hike", "hawkish", "tariff", "sanction")


def gold_note_event(title, cur):
    t = title.lower()
    if cur == "USD":
        if any(k in t for k in INVERSE):
            return ("اثر روی طلا: عدد بالاتر از پیش‌بینی (بیکاری بیشتر) معمولاً دلار را ضعیف و طلا را بالا می‌برد؛ "
                    "پایین‌تر از پیش‌بینی معمولاً برعکس.")
        return ("اثر روی طلا: عدد بالاتر از پیش‌بینی (به نفع دلار) معمولاً فشار نزولی روی طلا؛ "
                "پایین‌تر از پیش‌بینی معمولاً حمایت برای طلا.")
    return "اثر روی طلا: غیرمستقیم است و از مسیر قدرت دلار می‌آید؛ بیشتر روی جفت‌ارز %s اثر مستقیم دارد." % cur


def gold_note_news(title):
    t = title.lower()
    if any(k in t for k in RISK_UP):
        return "اثر روی طلا: تنش و ریسک ژئوپلیتیک معمولاً به نفع طلا (پناهگاه امن)."
    if any(k in t for k in RATE_DOWN):
        return "اثر روی طلا: کاهش نرخ بهره یا لحن نرم معمولاً مثبت برای طلا."
    if any(k in t for k in RATE_UP):
        return "اثر روی طلا: لحن سخت‌گیرانه یا تعرفه/تحریم معمولاً فشار روی طلا یا نوسان شدید؛ واکنش بازار را ببینید."
    return "اثر روی طلا: جهت مشخص نیست؛ واکنش اولیه قیمت را ببینید."


def load_calendar():
    now = datetime.now(timezone.utc)
    events = []
    for i, url in enumerate(CALENDAR_URLS):
        if i == 1 and now.weekday() < 4:
            continue
        try:
            items = json.loads(fetch(url).decode("utf-8"))
        except Exception as e:
            print("تقویم دانلود نشد (%s): %s" % (url.split("/")[-1], e))
            continue
        for it in items:
            try:
                if it.get("impact") not in IMPACTS:
                    continue
                cur = it.get("country", "")
                if CURRENCIES and cur not in CURRENCIES:
                    continue
                t = datetime.fromisoformat(it["date"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                events.append({
                    "key": "%s|%s|%s" % (it["date"], cur, it.get("title")),
                    "time": t, "cur": cur, "title": it.get("title", ""),
                    "forecast": it.get("forecast", ""), "previous": it.get("previous", ""),
                    "link": it.get("url") or CALENDAR_LINK,
                })
            except Exception:
                continue
    events.sort(key=lambda e: e["time"])
    return events


def check_calendar(events, state, now=None):
    now = now or datetime.now(timezone.utc)
    done = set(state["events"])
    sent = 0
    for ev in events:
        if ev["key"] in done:
            continue
        delta = (ev["time"] - now).total_seconds()
        if 0 < delta <= WINDOW_MINUTES * 60:
            mins = max(1, int(round(delta / 60)))
            local = ev["time"].astimezone(TEHRAN).strftime("%H:%M")
            lines = ["⏰ %d دقیقه دیگر خبر مهم" % mins,
                     "%s | %s" % (ev["cur"], ev["title"]),
                     "ساعت %s به وقت تهران" % local]
            extra = []
            if ev["forecast"]:
                extra.append("پیش‌بینی: %s" % ev["forecast"])
            if ev["previous"]:
                extra.append("قبلی: %s" % ev["previous"])
            if extra:
                lines.append(" | ".join(extra))
            lines.append(gold_note_event(ev["title"], ev["cur"]))
            lines.append(CALENDAR_LINK)
            if send_telegram("\n".join(lines)):
                state["events"].append(ev["key"])
                sent += 1
        elif delta <= 0:
            if ev["key"] not in done and delta < -600:
                state["events"].append(ev["key"])
    return sent


def strip_ns(tag):
    return tag.split("}", 1)[-1]


def parse_feed(raw):
    out = []
    root = ET.fromstring(raw)
    for node in root.iter():
        if strip_ns(node.tag) not in ("item", "entry"):
            continue
        title = link = guid = ""
        for ch in node:
            n = strip_ns(ch.tag)
            if n == "title":
                title = (ch.text or "").strip()
            elif n == "link":
                link = (ch.text or "").strip() or ch.attrib.get("href", "")
            elif n in ("guid", "id"):
                guid = (ch.text or "").strip()
        if title and link:
            out.append((guid or link, title, link))
    return out


def check_news(state):
    seen = set(state["news"])
    seeding = not state["seeded"]
    sent = 0
    for name, url in NEWS_FEEDS.items():
        try:
            items = parse_feed(fetch(url))
        except Exception as e:
            print("خطا در %s: %s" % (name, e))
            continue
        for uid, title, link in items:
            key = "%s|%s" % (name, uid)
            if key in seen:
                continue
            seen.add(key)
            state["news"].append(key)
            if seeding:
                continue
            if KEYWORD_RE.search(title):
                msg = "🚨 خبر فوری - %s\n%s\n%s\n%s" % (name, title, gold_note_news(title), link)
                send_telegram(msg)
                sent += 1
    state["seeded"] = True
    return sent


def main():
    if "--test" in sys.argv:
        ok = send_telegram("✅ تست: ربات آلارم اخبار درست وصل است.")
        print("ارسال شد" if ok else "ارسال نشد")
        return
    state = load_state()
    events = load_calendar()
    n1 = check_calendar(events, state)
    n2 = check_news(state)
    save_state(state)
    print("رویداد تقویم ارسال‌شده: %d | خبر فوری ارسال‌شده: %d" % (n1, n2))


if __name__ == "__main__":
    main()

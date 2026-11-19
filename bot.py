#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات تلگرام: ۵ دقیقه بعد از اعلام خبر، واکنش قیمت را می‌سنجد.
اگر حرکت چشمگیر بود پیام می‌دهد؛ اگر چارت رنج بود هیچ پیامی نمی‌آید.
"""
import email.utils
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ============================ تنظیمات ============================
IMPACTS = {"High"}
STRICT_EVENTS = True       # True = فقط خبرهای بزرگ و حرکت‌ساز
STRICT_NEWS = True         # True = فقط خبرهای فوری بزرگ
MAX_NEWS_PER_RUN = 3
CURRENCIES = {"USD", "AUD", "CHF", "GBP", "NZD", "EUR", "CAD", "JPY"}

REACT_MIN = 5              # چند دقیقه بعد از خبر، واکنش قیمت سنجیده شود
MAX_LATE_MIN = 30          # اگر بیشتر از این دیر شد، رها کن

# معیار «حرکت چشمگیر»: هر دو شرط باید برقرار باشد
RATIO_MIN = 2.5            # نوسان ۵ دقیقه‌ی بعد از خبر حداقل چند برابر نوسان معمول ۳ ساعت قبل
MIN_PCT_FX = 0.08          # حداقل دامنه‌ی نوسان جفت‌ارز (درصد). حدود ۹ پیپ در EUR/USD
MIN_PCT_GOLD = 0.15        # حداقل دامنه‌ی نوسان طلا (درصد)

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

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
TEHRAN = timezone(timedelta(hours=3, minutes=30))
UA = "Mozilla/5.0 (compatible; NewsAlarmBot/3.0)"

CUR_FA = {
    "USD": "دلار آمریکا", "EUR": "یورو", "GBP": "پوند", "JPY": "ین",
    "CHF": "فرانک سوئیس", "AUD": "دلار استرالیا", "NZD": "دلار نیوزلند", "CAD": "دلار کانادا",
}

# کلید: (نمادهای Yahoo، نام نمایشی، ارز پایه، ارز مقابل، اندازه‌ی پیپ)
INSTR = {
    "EURUSD": (["EURUSD=X"], "EUR/USD", "EUR", "USD", 0.0001),
    "GBPUSD": (["GBPUSD=X"], "GBP/USD", "GBP", "USD", 0.0001),
    "USDJPY": (["USDJPY=X"], "USD/JPY", "USD", "JPY", 0.01),
    "USDCHF": (["USDCHF=X"], "USD/CHF", "USD", "CHF", 0.0001),
    "AUDUSD": (["AUDUSD=X"], "AUD/USD", "AUD", "USD", 0.0001),
    "NZDUSD": (["NZDUSD=X"], "NZD/USD", "NZD", "USD", 0.0001),
    "USDCAD": (["USDCAD=X"], "USD/CAD", "USD", "CAD", 0.0001),
    "GOLD": (["XAUUSD=X", "GC=F"], "طلا (XAU/USD)", "XAU", "USD", None),
}
CUR_PAIRS = {
    "USD": ["EURUSD", "USDJPY", "GBPUSD"], "EUR": ["EURUSD"], "GBP": ["GBPUSD"], "JPY": ["USDJPY"],
    "CHF": ["USDCHF"], "AUD": ["AUDUSD"], "NZD": ["NZDUSD"], "CAD": ["USDCAD"],
}


# ============================ ابزارها ============================
def fetch(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    text = text[:3800]
    if not token or not chat:
        print("[بدون تلگرام] " + text)
        return True
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        fetch("https://api.telegram.org/bot%s/sendMessage" % token, data=data)
        return True
    except Exception as e:
        print("خطا در ارسال تلگرام: %s" % e)
        return False


def translate_fa(text):
    text = (text or "").strip()
    if not text:
        return ""
    try:
        q = urllib.parse.urlencode({"client": "gtx", "sl": "en", "tl": "fa", "dt": "t", "q": text[:900]})
        raw = fetch("https://translate.googleapis.com/translate_a/single?" + q, timeout=15)
        data = json.loads(raw.decode("utf-8"))
        return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
    except Exception as e:
        print("ترجمه نشد: %s" % e)
        return ""


def clean_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("events", [])
    s.setdefault("news", [])
    s.setdefault("seeded", False)
    s.setdefault("pending", [])
    return s


def save_state(s):
    s["events"] = s["events"][-1500:]
    s["news"] = s["news"][-4000:]
    s["pending"] = s["pending"][-50:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f)


# ============================ داده‌ی قیمت ============================
_BARS = {}


def get_bars(key):
    """کندل‌های ۱ دقیقه‌ای (ts, high, low, close). اگر نشد لیست خالی."""
    if key in _BARS:
        return _BARS[key]
    bars = []
    for sym in INSTR[key][0]:
        for host in ("query1", "query2"):
            try:
                url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?interval=1m&range=2d" % (
                    host, urllib.parse.quote(sym))
                d = json.loads(fetch(url).decode("utf-8"))
                res = d["chart"]["result"][0]
                ts = res["timestamp"]
                q = res["indicators"]["quote"][0]
                for i, t in enumerate(ts):
                    h, l, c = q["high"][i], q["low"][i], q["close"][i]
                    if h is None or l is None or c is None:
                        continue
                    bars.append((int(t), float(h), float(l), float(c)))
                if bars:
                    break
            except Exception as e:
                print("قیمت %s از %s گرفته نشد: %s" % (sym, host, e))
        if bars:
            break
    bars.sort()
    _BARS[key] = bars
    return bars


def eval_instr(key, t0):
    """واکنش قیمت در ۵ دقیقه‌ی بعد از t0 (epoch). اگر داده کافی نبود None."""
    bars = get_bars(key)
    if not bars:
        return None
    pre = [b for b in bars if b[0] < t0 and t0 - b[0] <= 600]
    win = [b for b in bars if t0 <= b[0] < t0 + 300]
    if not pre or len(win) < 3:
        return None
    pre_c = pre[-1][3]
    hi = max(max(b[1] for b in win), pre_c)
    lo = min(min(b[2] for b in win), pre_c)
    rng = hi - lo
    net = win[-1][3] - pre_c
    buckets = {}
    for b in bars:
        if t0 - 3 * 3600 <= b[0] < t0 - 600:
            buckets.setdefault(b[0] // 300, []).append(b)
    ranges = sorted(max(x[1] for x in v) - min(x[2] for x in v) for v in buckets.values() if len(v) >= 3)
    base = ranges[len(ranges) // 2] if len(ranges) >= 6 else None
    ratio = (rng / base) if (base and base > 0) else None
    pct = rng / pre_c * 100.0
    floor = MIN_PCT_GOLD if key == "GOLD" else MIN_PCT_FX
    sig = pct >= floor and (ratio is None or ratio >= RATIO_MIN)
    return {"key": key, "net": net, "rng": rng, "pct": pct, "ratio": ratio, "sig": sig, "pre": pre_c}


def strength(cur, key, net):
    """+1 اگر این حرکت یعنی ارز cur قوی‌تر شده، -1 اگر ضعیف‌تر."""
    if net == 0:
        return 0
    sign = 1 if net > 0 else -1
    base, quote = INSTR[key][2], INSTR[key][3]
    if cur == base:
        return sign
    if cur == quote:
        return -sign
    return 0


def fmt_amount(key, v):
    if key == "GOLD":
        return "%.1f دلار" % abs(v)
    return "%.0f پیپ" % (abs(v) / INSTR[key][4])


def fmt_line(r):
    key = r["key"]
    arrow = "↑" if r["net"] > 0 else ("↓" if r["net"] < 0 else "→")
    txt = "• %s: %s %s | دامنه‌ی نوسان %s" % (INSTR[key][1], arrow, fmt_amount(key, r["net"]),
                                            fmt_amount(key, r["rng"]))
    if r["ratio"]:
        txt += " (%.1f برابر معمول)" % r["ratio"]
    return txt + (" 🔴" if r["sig"] else "")


def reaction(curs, t0, gold_triggers):
    """حرکت قیمت را برای فهرست ارزها می‌سنجد. خروجی: (ok, triggered, خطوط، نتیجه‌ها)"""
    keys = []
    for c in curs:
        for k in CUR_PAIRS.get(c, []):
            if k not in keys:
                keys.append(k)
    if not keys:
        keys = ["EURUSD", "USDJPY"]
    results = {k: eval_instr(k, t0) for k in keys + ["GOLD"]}
    main = [results[k] for k in keys if results[k]]
    gold = results["GOLD"]
    ok = bool(main) or bool(gold)
    triggered = any(r["sig"] for r in main) or bool(gold_triggers and gold and gold["sig"])
    lines = [fmt_line(r) for r in main]
    if gold:
        lines.append(fmt_line(gold))
    verdicts = []
    for c in curs:
        total = 0
        used = False
        for k in CUR_PAIRS.get(c, []):
            r = results.get(k)
            if r and r["sig"]:
                total += strength(c, k, r["net"])
                used = True
        if c == "USD" and gold and gold["sig"]:
            total += strength("USD", "GOLD", gold["net"])
            used = True
        if used:
            if total > 0:
                verdicts.append("✅ %s در این واکنش تقویت شد." % CUR_FA[c])
            elif total < 0:
                verdicts.append("✅ %s در این واکنش تضعیف شد." % CUR_FA[c])
            else:
                verdicts.append("⚖️ %s: جفت‌ارزها ناهمسو بودند و نتیجه مبهم است." % CUR_FA[c])
    if gold and gold["sig"]:
        verdicts.append("🥇 طلا %s." % ("بالا رفت" if gold["net"] > 0 else "پایین آمد"))
    return ok, triggered, lines, verdicts


# ============================ کاتالوگ خبرهای حرکت‌ساز تقویم ============================
CATALOG = [
    (r"non-?farm|nfp", "اشتغال غیرکشاورزی آمریکا (NFP)",
     "تعداد شغل‌های جدید ساخته‌شده در آمریکا در ماه گذشته؛ مهم‌ترین گزارش بازار کار."),
    (r"unemployment claims|jobless claims", "درخواست‌های بیمه بیکاری",
     "تعداد افرادی که این هفته برای بیمه بیکاری ثبت‌نام کرده‌اند؛ عدد بالاتر یعنی بازار کار ضعیف‌تر."),
    (r"unemployment rate", "نرخ بیکاری",
     "درصد افراد بیکار در جستجوی کار؛ عدد بالاتر یعنی اقتصاد ضعیف‌تر."),
    (r"employment change|employment rate|employment$", "تغییر اشتغال",
     "تعداد شغل‌های جدید؛ عدد بالاتر یعنی بازار کار قوی‌تر."),
    (r"cpi|consumer price", "شاخص قیمت مصرف‌کننده (CPI / تورم)",
     "میزان افزایش قیمت‌ها؛ تورم بالاتر یعنی احتمال بالا رفتن نرخ بهره بیشتر می‌شود."),
    (r"pce", "شاخص قیمت PCE (تورم)", "معیار تورمی که فدرال رزرو بیشتر از همه به آن نگاه می‌کند."),
    (r"ppi|producer price", "شاخص قیمت تولیدکننده (PPI)", "تورم در سطح تولید؛ پیش‌نگر تورم مصرف‌کننده."),
    (r"gdp", "تولید ناخالص داخلی (GDP)", "اندازه‌ی رشد اقتصاد کشور؛ عدد بالاتر یعنی اقتصاد قوی‌تر."),
    (r"retail sales", "فروش خرده‌فروشی", "میزان خرید مردم؛ بخش بزرگی از اقتصاد را نشان می‌دهد."),
    (r"ism|pmi", "شاخص مدیران خرید (PMI / ISM)", "سلامت بخش صنعت یا خدمات؛ بالای ۵۰ یعنی رشد."),
    (r"speaks|speech|testifies|testimony|press conference", "سخنرانی یا کنفرانس خبری مقام بانک مرکزی",
     "اشاره‌ی مقام بانک مرکزی به آینده‌ی نرخ بهره می‌تواند بازار را شدیداً تکان دهد."),
    (r"minutes", "صورتجلسه‌ی بانک مرکزی", "جزئیات بحث جلسه‌ی قبلی بانک مرکزی و سرنخ تصمیم بعدی."),
    (r"fomc|federal funds|rate statement|rate decision|cash rate|bank rate|refinancing rate|policy rate|"
     r"monetary policy|overnight rate|interest rate", "تصمیم نرخ بهره‌ی بانک مرکزی",
     "اعلام نرخ بهره و بیانیه‌ی بانک مرکزی؛ مهم‌ترین عامل تعیین‌کننده‌ی قیمت یک ارز."),
]
CATALOG = [(re.compile(p, re.IGNORECASE), n, d) for p, n, d in CATALOG]


def match_event(title):
    for rx, name, desc in CATALOG:
        if rx.search(title or ""):
            return name, desc
    return None


# ============================ تقویم اقتصادی ============================
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
                title = it.get("title", "")
                info = match_event(title)
                if STRICT_EVENTS and not info:
                    continue
                t = datetime.fromisoformat(it["date"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                events.append({
                    "key": "%s|%s|%s" % (it["date"], cur, title),
                    "time": t, "cur": cur, "title": title, "info": info,
                    "forecast": it.get("forecast", ""), "previous": it.get("previous", ""),
                    "link": it.get("url") or CALENDAR_LINK,
                })
            except Exception:
                continue
    events.sort(key=lambda e: e["time"])
    return events


def event_message(ev, age_min, lines, verdicts):
    cur = ev["cur"]
    name_fa, desc_fa = ev["info"] or (ev["title"], "خبر مهم اقتصادی.")
    local = ev["time"].astimezone(TEHRAN).strftime("%H:%M")
    out = [
        "📣 واکنش بازار | %s" % CUR_FA.get(cur, cur),
        "📌 %s" % name_fa,
        "(%s)" % ev["title"],
        "🕒 ساعت %s به وقت تهران (%d دقیقه پیش)" % (local, age_min),
        "",
        "📖 توضیح: %s" % desc_fa,
    ]
    extra = []
    if ev["forecast"]:
        extra.append("پیش‌بینی: %s" % ev["forecast"])
    if ev["previous"]:
        extra.append("قبلی: %s" % ev["previous"])
    if extra:
        out.append("📊 " + " | ".join(extra) + " (عدد واقعی را در لینک ببین)")
    out += ["", "📈 حرکت قیمت در ۵ دقیقه‌ی اول بعد از خبر (🔴 = چشمگیر):"] + lines + [""] + verdicts + ["", ev["link"]]
    return "\n".join(out)


def process_events(events, state, now=None):
    now = now or datetime.now(timezone.utc)
    done = set(state["events"])
    sent = 0
    for ev in events:
        if ev["key"] in done:
            continue
        age = (now - ev["time"]).total_seconds()
        if age < (REACT_MIN * 60 + 30):
            continue
        if age > MAX_LATE_MIN * 60:
            state["events"].append(ev["key"])
            continue
        t0 = int(ev["time"].timestamp())
        ok, triggered, lines, verdicts = reaction([ev["cur"]], t0, gold_triggers=(ev["cur"] == "USD"))
        age_min = int(age // 60)
        if not ok:
            if age > 24 * 60:  # داده‌ی قیمت هیچ‌وقت نیامد
                send_telegram("⚠️ خبر «%s» (%s) اعلام شد ولی داده‌ی قیمت را نتوانستم بگیرم؛ چارت را خودت ببین.\n%s"
                              % (ev["title"], ev["cur"], ev["link"]))
                state["events"].append(ev["key"])
            continue
        if not triggered:
            state["events"].append(ev["key"])  # چارت رنج؛ پیام نمی‌دهیم
            continue
        if send_telegram(event_message(ev, age_min, lines, verdicts)):
            state["events"].append(ev["key"])
            sent += 1
    return sent


# ============================ اخبار فوری ============================
TOPIC_RE = re.compile(
    r"\bfed\b|federal reserve|fomc|powell|\becb\b|lagarde|\bboe\b|bank of england|bailey|\bboj\b|bank of japan|"
    r"ueda|\bsnb\b|\brba\b|\brbnz\b|\bboc\b|bank of canada|non-?farm|payrolls|\bnfp\b|\bcpi\b|inflation|"
    r"\bgdp\b|opec|tariff|sanction|treasury|intervention|central bank|rate decision|interest rate|"
    r"iran|israel|russia|ukraine|china|taiwan|north korea|trump|white house", re.IGNORECASE)
ACTION_RE = re.compile(
    r"rate cut|rate hike|cuts rates|raises rates|hikes|slashes|holds rates|surprise|unexpected|shock|"
    r"beats|misses|soars|plunges|surges|tumbles|spikes|crashes|emergency|announces|imposes|orders|"
    r"ceasefire|invasion|strike|retaliat|escalat|default|shutdown|resign|collapse", re.IGNORECASE)
EMERGENCY_RE = re.compile(
    r"breaking|flash|emergency|intervention|missile|invasion|nuclear|airstrike|default|ceasefire", re.IGNORECASE)

CUR_KEYS = {
    "USD": r"\bfed\b|federal reserve|fomc|powell|dollar|treasur|u\.s\.|united states|trump|wall street",
    "EUR": r"\becb\b|lagarde|\beuro\b|eurozone|euro zone|germany",
    "GBP": r"\bboe\b|bank of england|bailey|sterling|\bpound\b|\buk\b|britain",
    "JPY": r"\bboj\b|bank of japan|ueda|\byen\b|japan",
    "CHF": r"\bsnb\b|swiss|franc",
    "AUD": r"\brba\b|australia|aussie",
    "NZD": r"\brbnz\b|new zealand|kiwi",
    "CAD": r"\bboc\b|bank of canada|canad|loonie",
}
CUR_KEYS = {k: re.compile(v, re.IGNORECASE) for k, v in CUR_KEYS.items()}


def news_passes(title, desc):
    text = title + " " + desc
    if not STRICT_NEWS:
        return bool(TOPIC_RE.search(text))
    if EMERGENCY_RE.search(title):
        return True
    return bool(TOPIC_RE.search(title) and ACTION_RE.search(title))


def detect_currencies(text):
    return [c for c, rx in CUR_KEYS.items() if rx.search(text)][:3]


def strip_ns(tag):
    return tag.split("}", 1)[-1]


def parse_pub(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def parse_feed(raw):
    out = []
    root = ET.fromstring(raw)
    for node in root.iter():
        if strip_ns(node.tag) not in ("item", "entry"):
            continue
        title = link = guid = desc = pub = ""
        for ch in node:
            n = strip_ns(ch.tag)
            if n == "title":
                title = (ch.text or "").strip()
            elif n == "link":
                link = (ch.text or "").strip() or ch.attrib.get("href", "")
            elif n in ("guid", "id"):
                guid = (ch.text or "").strip()
            elif n in ("description", "summary", "encoded", "content") and not desc:
                desc = clean_html(ch.text or "")
            elif n in ("pubDate", "published", "updated", "date") and not pub:
                pub = (ch.text or "").strip()
        if title and link:
            out.append((guid or link, title, link, desc, parse_pub(pub)))
    return out


def news_message(p, age_min, lines, verdicts):
    title, desc = p["title"], p["desc"]
    title_fa = translate_fa(title)
    desc_short = desc[:350]
    if desc_short and desc_short.lower().startswith(title.lower()[:40]):
        desc_short = ""
    desc_fa = translate_fa(desc_short) if desc_short else ""
    out = ["🚨 خبر فوری و واکنش بازار | %s" % p["source"], "", "📌 %s" % (title_fa or title)]
    if title_fa:
        out.append("(%s)" % title)
    out.append("🕒 %d دقیقه پیش منتشر شد" % age_min)
    if desc_fa:
        out += ["", "📖 شرح: %s" % desc_fa]
    elif desc_short:
        out += ["", "📖 شرح (ترجمه در دسترس نبود): %s" % desc_short]
    out += ["", "📈 حرکت قیمت در ۵ دقیقه‌ی اول بعد از خبر (🔴 = چشمگیر):"] + lines + [""] + verdicts + ["", p["link"]]
    return "\n".join(out)


def check_news(state, now=None):
    now = now or datetime.now(timezone.utc)
    seen = set(state["news"])
    seeding = not state["seeded"]
    for name, url in NEWS_FEEDS.items():
        try:
            items = parse_feed(fetch(url))
        except Exception as e:
            print("خطا در %s: %s" % (name, e))
            continue
        for uid, title, link, desc, pub in items:
            key = "%s|%s" % (name, uid)
            if key in seen:
                continue
            seen.add(key)
            state["news"].append(key)
            if seeding or not news_passes(title, desc):
                continue
            t0 = pub or now
            if (now - t0).total_seconds() > MAX_LATE_MIN * 60:
                continue
            state["pending"].append({
                "key": key, "t0": int(t0.timestamp()), "source": name, "title": title,
                "desc": desc, "link": link, "curs": detect_currencies(title + " " + desc),
            })
    state["seeded"] = True


def process_pending(state, now=None):
    now = now or datetime.now(timezone.utc)
    keep, sent = [], 0
    for p in state["pending"]:
        age = now.timestamp() - p["t0"]
        if age < REACT_MIN * 60 + 30:
            keep.append(p)
            continue
        if age > MAX_LATE_MIN * 60:
            continue
        if sent >= MAX_NEWS_PER_RUN:
            keep.append(p)
            continue
        ok, triggered, lines, verdicts = reaction(p["curs"], p["t0"], gold_triggers=True)
        if not ok:
            keep.append(p)  # داده‌ی قیمت هنوز نیامده؛ دوباره
            continue
        if not triggered:
            continue  # چارت رنج
        if send_telegram(news_message(p, int(age // 60), lines, verdicts)):
            sent += 1
        else:
            keep.append(p)
    state["pending"] = keep
    return sent


# ============================ اجرا ============================
def main():
    if "--test" in sys.argv:
        lines = ["✅ تست: ربات آلارم اخبار درست وصل است.", "", "تست داده‌ی قیمت:"]
        for key in ("EURUSD", "GOLD"):
            bars = get_bars(key)
            if bars:
                age = int((datetime.now(timezone.utc).timestamp() - bars[-1][0]) // 60)
                lines.append("• %s: %s (آخرین کندل %d دقیقه پیش)" % (INSTR[key][1], bars[-1][3], age))
            else:
                lines.append("• %s: ❌ داده‌ی قیمت گرفته نشد" % INSTR[key][1])
        ok = send_telegram("\n".join(lines))
        print("ارسال شد" if ok else "ارسال نشد")
        return
    state = load_state()
    events = load_calendar()
    n1 = process_events(events, state)
    check_news(state)
    n2 = process_pending(state)
    save_state(state)
    print("پیام رویداد: %d | پیام خبر فوری: %d | در انتظار: %d" % (n1, n2, len(state["pending"])))


if __name__ == "__main__":
    main()

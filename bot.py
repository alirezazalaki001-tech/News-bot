#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات تلگرام: ۱۰ دقیقه بعد از اعلام هر خبر مهم، حرکت قیمت ۲۸ جفت‌ارز + طلا + نزدک را می‌سنجد.
فقط اگر حرکت چشمگیر بود پیام می‌دهد؛ اگر چارت رنج بود پیامی نمی‌آید.
"""
import bisect
import email.utils
import html
import json
import os
import re
import statistics
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ============================ تنظیمات (قابل تغییر) ============================
IMPACTS = {"High"}
STRICT_EVENTS = True       # True = فقط خبرهای بزرگ تقویم
STRICT_NEWS = True         # True = فقط خبرهای فوری بزرگ
MAX_NEWS_PER_RUN = 3
CURRENCIES = {"USD", "AUD", "CHF", "GBP", "NZD", "EUR", "CAD", "JPY"}

REACT_MIN = 10             # چند دقیقه بعد از خبر، حرکت قیمت سنجیده شود
MAX_LATE_MIN = 45          # اگر بیشتر از این دیر شد، رها کن
GOLD_WAIT_MIN = 25         # برای داده‌ی طلا و نزدک (گاهی دیر می‌آید) تا این‌قدر صبر کن

# معیار «حرکت چشمگیر» (همه با هم)
RATIO_MIN = 2.0            # نوسان ۱۰ دقیقه‌ی بعد از خبر حداقل چند برابر نوسان معمول (۱.۵ = حساس‌تر ولی پیام الکی بیشتر)
VOL_RATIO_MIN = 1.5        # حجم معاملات (فقط طلا و نزدک) حداقل چند برابر معمول
NET_FRAC = 0.5             # حرکت باید یک‌جهته باشد: تغییر خالص حداقل این کسر از کل نوسان
MIN_PAIRS_AGREE = 2        # حداقل چند جفت‌ارز هم‌جهت برای تأیید حرکت یک ارز (جایگزین حجم در فارکس)
FLOOR_FX = 0.05            # حداقل نوسان جفت‌ارز (درصد)
FLOOR_GOLD = 0.10          # حداقل نوسان طلا (درصد)
FLOOR_NQ = 0.12            # حداقل نوسان نزدک (درصد)

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
UA = "Mozilla/5.0 (compatible; NewsAlarmBot/4.0)"
YUA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0 Safari/537.36")

CUR_FA = {
    "USD": "دلار آمریکا", "EUR": "یورو", "GBP": "پوند", "JPY": "ین",
    "CHF": "فرانک سوئیس", "AUD": "دلار استرالیا", "NZD": "دلار نیوزلند", "CAD": "دلار کانادا",
}

# ۷ جفت دلاری از Yahoo؛ بقیه‌ی ۲۸ جفت از روی آن‌ها محاسبه می‌شود. (نماد، معکوس؟)
USD_PAIRS = {
    "EUR": ("EURUSD=X", False), "GBP": ("GBPUSD=X", False), "AUD": ("AUDUSD=X", False),
    "NZD": ("NZDUSD=X", False), "JPY": ("USDJPY=X", True), "CHF": ("USDCHF=X", True),
    "CAD": ("USDCAD=X", True),
}
PAIRS = [
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "USDCAD",
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURNZD", "EURCAD",
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPNZD", "GBPCAD",
    "AUDJPY", "AUDCHF", "AUDNZD", "AUDCAD",
    "NZDJPY", "NZDCHF", "NZDCAD",
    "CADJPY", "CADCHF", "CHFJPY",
]
GOLD_SYMS = (["GC=F"], ["XAUUSD=X"])   # (اصلی با حجم، پشتیبان بدون حجم)
NQ_SYMS = (["NQ=F"], ["^NDX"])


# ============================ ابزارها ============================
def fetch(url, data=None, timeout=20, ua=UA):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": ua})
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
_CACHE = {}


def get_series(symbols):
    """کندل‌های ۱ دقیقه‌ای [(ts, close, volume)]. اگر نشد لیست خالی."""
    key = "|".join(symbols)
    if key in _CACHE:
        return _CACHE[key]
    out = []
    for sym in symbols:
        for host in ("query1", "query2"):
            try:
                url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?interval=1m&range=2d" % (
                    host, urllib.parse.quote(sym))
                d = json.loads(fetch(url, ua=YUA).decode("utf-8"))
                res = d["chart"]["result"][0]
                ts = res["timestamp"]
                q = res["indicators"]["quote"][0]
                cl = q["close"]
                vo = q.get("volume") or [None] * len(ts)
                for i, t in enumerate(ts):
                    if i >= len(cl) or cl[i] is None:
                        continue
                    out.append((int(t), float(cl[i]), float(vo[i] or 0) if i < len(vo) else 0.0))
                if out:
                    break
            except Exception as e:
                print("قیمت %s از %s گرفته نشد: %s" % (sym, host, e))
        if out:
            break
    out.sort()
    _CACHE[key] = out
    return out


def fx_panel():
    """(grid, {pair: [close...]}) برای ۲۸ جفت‌ارز، یا None اگر داده نبود."""
    if "panel" in _CACHE:
        return _CACHE["panel"]
    raw = {}
    for cur, (sym, inv) in USD_PAIRS.items():
        s = get_series([sym])
        if not s:
            _CACHE["panel"] = None
            return None
        raw[cur] = ({t: c for t, c, v in s}, inv)
    ts_all = sorted(set().union(*[set(d.keys()) for d, _ in raw.values()]))
    last, grid = {}, []
    vals = {c: [] for c in raw}
    for t in ts_all:
        for c, (d, inv) in raw.items():
            if t in d:
                last[c] = d[t]
        if len(last) < len(raw):
            continue
        grid.append(t)
        for c, (d, inv) in raw.items():
            p = last[c]
            vals[c].append(1.0 / p if inv else p)
    vals["USD"] = [1.0] * len(grid)
    pairs = {}
    for name in PAIRS:
        a, b = name[:3], name[3:]
        pairs[name] = [x / y for x, y in zip(vals[a], vals[b])]
    _CACHE["panel"] = (grid, pairs)
    return _CACHE["panel"]


def eval_move(ts, cl, vo, t0, floor):
    """سنجش حرکت از لحظه‌ی t0 تا REACT_MIN دقیقه بعد. None = داده نیست، {'incomplete':True} = هنوز کامل نشده."""
    if len(ts) < 50:
        return None
    win_sec = REACT_MIN * 60
    if ts[-1] < t0 + win_sec - 120:
        return {"incomplete": True}
    i0 = bisect.bisect_right(ts, t0) - 1
    if i0 < 0 or t0 - ts[i0] > 300:
        return None
    j_end = bisect.bisect_right(ts, t0 + win_sec)
    w = cl[i0 + 1:j_end]
    if len(w) < 4:
        return None
    p0 = cl[i0]
    hi, lo = max(max(w), p0), min(min(w), p0)
    rng = (hi - lo) / p0 * 100.0
    net_abs = w[-1] - p0
    net_pct = net_abs / p0 * 100.0
    # نوسان معمول: دامنه‌ی بازه‌های ۱۰ دقیقه‌ای ۴ ساعت قبل از خبر (میانه)
    b0 = bisect.bisect_left(ts, t0 - 4 * 3600)
    b1 = bisect.bisect_left(ts, t0 - win_sec)
    buckets = {}
    for k in range(b0, b1):
        buckets.setdefault(ts[k] // win_sec, []).append(k)
    ranges, vols = [], []
    for ks in buckets.values():
        if len(ks) < 6:
            continue
        seg = [cl[k] for k in ks]
        ranges.append((max(seg) - min(seg)) / seg[0] * 100.0)
        if vo:
            vols.append(sum(vo[k] for k in ks))
    base = statistics.median(ranges) if len(ranges) >= 8 else None
    ratio = (rng / base) if (base and base > 0) else None
    vratio = None
    if vo:
        wv = sum(vo[i0 + 1:j_end])
        mv = statistics.median(vols) if len(vols) >= 8 else None
        if wv > 0 and mv and mv > 0:
            vratio = wv / mv
    directional = rng > 0 and abs(net_pct) >= NET_FRAC * rng
    sig = rng >= floor and (ratio is None or ratio >= RATIO_MIN) and directional
    return {"net_abs": net_abs, "net_pct": net_pct, "rng": rng, "ratio": ratio, "vratio": vratio,
            "sig": sig, "vol_ok": (vratio is None or vratio >= VOL_RATIO_MIN), "p0": p0}


def eval_asset(syms, t0, floor):
    inc = False
    for symbols, use_vol in ((syms[0], True), (syms[1], False)):
        s = get_series(symbols)
        if not s:
            continue
        r = eval_move([x[0] for x in s], [x[1] for x in s], [x[2] for x in s] if use_vol else None, t0, floor)
        if r and r.get("incomplete"):
            inc = True
            continue
        if r:
            return r
    return {"incomplete": True} if inc else None


def strength(cur, pair, net):
    if net == 0:
        return 0
    sign = 1 if net > 0 else -1
    if cur == pair[:3]:
        return sign
    if cur == pair[3:]:
        return -sign
    return 0


def pair_line(name, r):
    pip = 0.01 if "JPY" in name else 0.0001
    arrow = "↑" if r["net_abs"] > 0 else "↓"
    txt = "• %s/%s: %s %.0f پیپ (%.2f%%)" % (name[:3], name[3:], arrow, abs(r["net_abs"]) / pip, abs(r["net_pct"]))
    if r["ratio"]:
        txt += " | نوسان %.1f برابر معمول" % r["ratio"]
    return txt + (" 🔴" if r["sig"] else "")


def asset_line(label, unit, r):
    arrow = "↑" if r["net_abs"] > 0 else "↓"
    txt = "• %s: %s %.1f %s (%.2f%%)" % (label, arrow, abs(r["net_abs"]), unit, abs(r["net_pct"]))
    if r["ratio"]:
        txt += " | نوسان %.1f برابر" % r["ratio"]
    if r["vratio"] is not None:
        txt += " | حجم %.1f برابر معمول" % r["vratio"]
    else:
        txt += " | حجم نامشخص"
    return txt + (" 🔴" if (r["sig"] and r["vol_ok"]) else "")


def reaction(curs, t0, want_gold, want_nq, age):
    """خروجی: (وضعیت, خطوط, نتیجه‌گیری)   وضعیت: send | skip | wait | nodata"""
    cur_list = curs or ["USD"]
    panel = fx_panel()
    if panel is None:
        return "nodata", [], []
    grid, series = panel
    res, inc = {}, 0
    for p in PAIRS:
        if not any(c in (p[:3], p[3:]) for c in cur_list):
            continue
        r = eval_move(grid, series[p], None, t0, FLOOR_FX)
        if r is None:
            continue
        if r.get("incomplete"):
            inc += 1
            continue
        res[p] = r
    if not res:
        return ("wait" if inc else "nodata"), [], []

    gold = nq = None
    waiting = False
    if want_gold:
        gold = eval_asset(GOLD_SYMS, t0, FLOOR_GOLD)
        if gold and gold.get("incomplete"):
            gold, waiting = None, True
    if want_nq:
        nq = eval_asset(NQ_SYMS, t0, FLOOR_NQ)
        if nq and nq.get("incomplete"):
            nq, waiting = None, True
    if waiting and age < GOLD_WAIT_MIN * 60:
        return "wait", [], []

    verdicts, move_any = [], False
    for c in cur_list:
        ups = dns = 0
        for p, r in res.items():
            if r["sig"]:
                s = strength(c, p, r["net_abs"])
                if s > 0:
                    ups += 1
                elif s < 0:
                    dns += 1
        total = sum(1 for p in res if c in (p[:3], p[3:]))
        if max(ups, dns) >= MIN_PAIRS_AGREE and ups != dns:
            move_any = True
            if ups > dns:
                verdicts.append("✅ %s در این ۱۰ دقیقه تقویت شد (%d از %d جفت‌ارز هم‌جهت)." % (CUR_FA[c], ups, total))
            else:
                verdicts.append("✅ %s در این ۱۰ دقیقه تضعیف شد (%d از %d جفت‌ارز هم‌جهت)." % (CUR_FA[c], dns, total))

    gold_sig = bool(gold and gold["sig"] and gold["vol_ok"])
    nq_sig = bool(nq and nq["sig"] and nq["vol_ok"])
    if gold_sig:
        verdicts.append("🥇 طلا %s." % ("بالا رفت" if gold["net_abs"] > 0 else "پایین آمد"))
    if nq_sig:
        verdicts.append("📊 نزدک %s." % ("بالا رفت" if nq["net_abs"] > 0 else "پایین آمد"))
    if not (move_any or gold_sig or nq_sig):
        return "skip", [], []

    lines = []
    ordered = sorted(res.items(), key=lambda kv: (not kv[1]["sig"], -abs(kv[1]["net_pct"])))
    for p, r in ordered[:6]:
        lines.append(pair_line(p, r))
    if gold:
        lines.append(asset_line("طلا (XAU/USD)", "دلار", gold))
    if nq:
        lines.append(asset_line("نزدک (NQ)", "پوینت", nq))
    return "send", lines, verdicts


# ============================ کاتالوگ خبرهای حرکت‌ساز تقویم ============================
CATALOG = [
    (r"non-?farm|nfp", "اشتغال غیرکشاورزی آمریکا (NFP)",
     "تعداد شغل‌های جدید ساخته‌شده در آمریکا در ماه گذشته؛ مهم‌ترین گزارش بازار کار."),
    (r"unemployment claims|jobless claims", "درخواست‌های بیمه بیکاری",
     "تعداد افرادی که این هفته برای بیمه بیکاری ثبت‌نام کرده‌اند؛ عدد بالاتر یعنی بازار کار ضعیف‌تر."),
    (r"unemployment rate", "نرخ بیکاری", "درصد افراد بیکار در جستجوی کار؛ عدد بالاتر یعنی اقتصاد ضعیف‌تر."),
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
    groups = {}
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
                key = "%s|%s" % (t.isoformat(), cur)
                g = groups.setdefault(key, {"key": key, "time": t, "cur": cur, "items": [],
                                            "link": it.get("url") or CALENDAR_LINK})
                g["items"].append({"title": title, "info": info, "forecast": it.get("forecast", ""),
                                   "previous": it.get("previous", "")})
            except Exception:
                continue
    events = sorted(groups.values(), key=lambda e: e["time"])
    return events


def event_message(ev, age_min, lines, verdicts):
    cur = ev["cur"]
    local = ev["time"].astimezone(TEHRAN).strftime("%H:%M")
    out = ["📣 خبر و واکنش بازار | %s" % CUR_FA.get(cur, cur),
           "🕒 ساعت %s به وقت تهران (%d دقیقه پیش اعلام شد)" % (local, age_min), ""]
    for it in ev["items"][:4]:
        name_fa, desc_fa = it["info"] or (it["title"], "خبر مهم اقتصادی.")
        out.append("📌 %s" % name_fa)
        out.append("(%s)" % it["title"])
        out.append("📖 %s" % desc_fa)
        extra = []
        if it["forecast"]:
            extra.append("پیش‌بینی: %s" % it["forecast"])
        if it["previous"]:
            extra.append("قبلی: %s" % it["previous"])
        if extra:
            out.append("📊 " + " | ".join(extra))
        out.append("")
    out.append("📈 تغییر قیمت از لحظه‌ی خبر تا %d دقیقه بعد (🔴 = چشمگیر):" % REACT_MIN)
    out += lines + [""] + verdicts + ["", "عدد واقعی خبر را در لینک ببین:", ev["link"]]
    return "\n".join(out)


def process_events(events, state, now=None):
    now = now or datetime.now(timezone.utc)
    done = set(state["events"])
    sent = 0
    for ev in events:
        if ev["key"] in done:
            continue
        age = (now - ev["time"]).total_seconds()
        if age < REACT_MIN * 60 + 30:
            continue
        if age > MAX_LATE_MIN * 60:
            state["events"].append(ev["key"])
            continue
        cur = ev["cur"]
        status, lines, verdicts = reaction([cur], int(ev["time"].timestamp()), cur == "USD", cur == "USD", age)
        if status == "wait":
            continue
        if status == "nodata":
            if age > GOLD_WAIT_MIN * 60:
                send_telegram("⚠️ خبر %s (%s) اعلام شد ولی داده‌ی قیمت را نتوانستم بگیرم؛ چارت را خودت ببین.\n%s"
                              % (CUR_FA.get(cur, cur), ev["items"][0]["title"], ev["link"]))
                state["events"].append(ev["key"])
            continue
        if status == "skip":
            state["events"].append(ev["key"])
            continue
        if send_telegram(event_message(ev, int(age // 60), lines, verdicts)):
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
RISK_RE = re.compile(r"war|attack|missile|invasion|airstrike|nuclear|escalat|retaliat|strike|"
                     r"iran|israel|russia|ukraine|taiwan|north korea|tariff|sanction|emergency|stocks|nasdaq|wall street",
                     re.IGNORECASE)

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
    out += ["", "📈 تغییر قیمت از لحظه‌ی خبر تا %d دقیقه بعد (🔴 = چشمگیر):" % REACT_MIN] + lines + [""] + verdicts
    out += ["", p["link"]]
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
            text = title + " " + desc
            curs = detect_currencies(text)
            glob = (not curs) or ("USD" in curs) or bool(RISK_RE.search(text))
            state["pending"].append({
                "key": key, "t0": int(t0.timestamp()), "source": name, "title": title,
                "desc": desc, "link": link, "curs": curs, "glob": glob,
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
        status, lines, verdicts = reaction(p["curs"], p["t0"], p["glob"], p["glob"], age)
        if status == "wait" or (status == "nodata" and age < GOLD_WAIT_MIN * 60):
            keep.append(p)
            continue
        if status in ("skip", "nodata"):
            continue
        if send_telegram(news_message(p, int(age // 60), lines, verdicts)):
            sent += 1
        else:
            keep.append(p)
    state["pending"] = keep
    return sent


# ============================ اجرا ============================
def run_test():
    lines = ["✅ تست: ربات آلارم اخبار درست وصل است.", "", "تست داده‌ی قیمت (Yahoo):"]
    now = datetime.now(timezone.utc).timestamp()
    for sym in ("EURUSD=X", "USDJPY=X", "GC=F", "XAUUSD=X", "NQ=F", "^NDX"):
        s = get_series([sym])
        if not s:
            lines.append("• %s: ❌ داده نیامد" % sym)
        else:
            age = int((now - s[-1][0]) // 60)
            vol = sum(x[2] for x in s[-30:])
            lines.append("• %s: ✅ آخرین کندل %d دقیقه پیش | حجم: %s" % (sym, age, "دارد" if vol > 0 else "ندارد"))
    panel = fx_panel()
    lines.append("• ۲۸ جفت‌ارز: %s" % ("✅ ساخته شد" if panel else "❌ ساخته نشد"))
    ok = send_telegram("\n".join(lines))
    print("ارسال شد" if ok else "ارسال نشد")


def main():
    if "--test" in sys.argv:
        run_test()
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

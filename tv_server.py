"""
سيرفر مواعيد برامج التلفزيون (elcinema -> SQLite -> API)

الاستخدام:
    pip install requests beautifulsoup4 fastapi uvicorn
    python tv_server.py dump 1128          # يحفظ HTML الصفحة في page_1128.html للفحص
    python tv_server.py scrape 1128 1129   # يسحب القنوات ويخزنها في tv.db
    python tv_server.py discover           # يحاول يطلع أرقام القنوات من صفحة الدليل
    uvicorn tv_server:app --reload         # يشغل الـ API والواجهة ويسحب الجداول تلقائي

الـ parser شغال على ترتيب النص في الصفحة (مش على أسماء classes)،
فمش هيتأثر لو الموقع غيّر التصميم، لكن هيتأثر لو غيّر ترتيب العناصر.
الأوقات محفوظة بتوقيت القاهرة (بدون tzinfo).
"""
import datetime as dt
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time

from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

DB_PATH = "tv.db"
TZ = ZoneInfo("Africa/Cairo")


def cairo_now() -> dt.datetime:
    """الوقت الحالي بتوقيت القاهرة (بدون tzinfo) - السيرفر ممكن يكون على UTC."""
    return dt.datetime.now(TZ).replace(tzinfo=None)

URL = "https://elcinema.com/tvguide/{}/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TVGuideApp/0.1)"}
DELAY_SECONDS = 3  # تأخير بين الطلبات
# أرقام القنوات اللي تتسحب تلقائي. غيّرها من متغير بيئة CHANNELS (مثلاً "1128 1129")
CHANNELS = [int(x) for x in os.environ.get("CHANNELS", "1128").replace(",", " ").split()]
REFRESH_HOURS = 6  # أقدم عمر مسموح للبيانات قبل ما تتسحب تاني

DAYS = "الاحد|الاثنين|الثلاثاء|الاربعاء|الخميس|الجمعة|السبت"  # بعد التطبيع (_norm)
MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "مايو": 5, "يونيو": 6,
    "يوليو": 7, "اغسطس": 8, "سبتمبر": 9, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
}
_TIME = r"(\d{1,2}):(\d{2})\s*(صباحا|مساء)"
_DUR = r"\[?\s*(\d+)\s*دقيقة\s*\]?"
RE_DAY = re.compile(rf"^({DAYS})\s+(\d{{1,2}})\s+(\S+)$")
RE_TIME_LINE = re.compile(rf"^{_TIME}(?:\s*{_DUR})?$")
RE_DUR_LINE = re.compile(rf"^{_DUR}$")
RE_KIND = re.compile(r"^(فيلم|برنامج|مسلسل|مسرحية|تمثيلية)\s*\((\d{4})\)$")
RE_KIND_PREFIX = re.compile(r"^(برنامج|فيلم|مسلسل|مسرحية)\s+")


def _norm(s: str) -> str:
    """للمطابقة فقط: شيل التشكيل ووحّد الهمزات."""
    s = re.sub(r"[\u064B-\u0652]", "", s)
    return s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")


def _to_24h(h: int, m: int, ampm: str) -> int:
    return (h % 12 + (12 if ampm == "مساء" else 0)) * 60 + m


def _pick_date(day: int, month: int, today: dt.date) -> dt.date:
    """الصفحة مفيهاش سنة: اختار السنة اللي تخلي التاريخ أقرب لدلوقتي."""
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = dt.date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def parse_lines(lines, today=None):
    """lines: أسطر نص الصفحة. بترجّع قائمة برامج."""
    today = today or cairo_now().date()
    out = []
    base = None
    prev_min = None
    rolled = False

    for i, raw in enumerate(lines):
        line = _norm(raw.strip())

        m = RE_DAY.match(line)
        if m:
            month = MONTHS.get(m.group(3))
            if month:
                base = _pick_date(int(m.group(2)), month, today)
                prev_min, rolled = None, False
            continue

        m = RE_TIME_LINE.match(line)
        if not m or base is None:
            continue
        minute = _to_24h(int(m.group(1)), int(m.group(2)), m.group(3))

        title_idx = None
        if m.group(4):  # الشكل الغريب: العنوان قبل الوقت، والمدة في نفس السطر
            duration = int(m.group(4))
            if i > 0:
                title_idx = i - 1
                title = RE_KIND_PREFIX.sub("", lines[title_idx].strip())
        else:  # الشكل العادي: وقت، مدة، عنوان
            if i + 2 < len(lines) and RE_DUR_LINE.match(_norm(lines[i + 1].strip())):
                duration = int(RE_DUR_LINE.match(_norm(lines[i + 1].strip())).group(1))
                title_idx = i + 2
                title = lines[title_idx].strip()
            else:
                continue
        if title_idx is None:
            continue

        kind, year = None, None
        if title_idx + 1 < len(lines):
            k = RE_KIND.match(_norm(lines[title_idx + 1].strip()))
            if k:
                kind, year = k.group(1), int(k.group(2))

        # لو الوقت رجع لورا يبقى عدّينا منتصف الليل
        if prev_min is not None and minute < prev_min and not rolled:
            rolled = True
        prev_min = minute

        start = dt.datetime.combine(base, dt.time()) + dt.timedelta(
            days=1 if rolled else 0, minutes=minute
        )
        out.append({
            "title": title,
            "start": start,
            "end": start + dt.timedelta(minutes=duration),
            "kind": kind,
            "year": year,
        })
    return out


def parse_page(html: str, today=None):
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else None

    # عنوان -> رقم العمل على elcinema (لو متاح)
    work_ids = {}
    for a in soup.select('a[href*="/work/"]'):
        t = a.get_text(strip=True)
        mm = re.search(r"/work/(\d+)", a["href"])
        if t and mm:
            work_ids.setdefault(t, int(mm.group(1)))

    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    programs = parse_lines(lines, today)
    for p in programs:
        p["work_id"] = work_ids.get(p["title"])
    if not programs:
        raise RuntimeError("ملقيتش برامج في الصفحة - شكل الصفحة اتغير؟ جرّب dump وشوف الـ HTML")
    return name, programs


# ---------------- قاعدة البيانات ----------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS programs (
            channel_id INTEGER, start TEXT, end TEXT, title TEXT,
            kind TEXT, year INTEGER, work_id INTEGER,
            PRIMARY KEY (channel_id, start)
        );
        CREATE INDEX IF NOT EXISTS idx_prog_time ON programs(start, end);
    """)
    return con


def scrape_channel(con, channel_id: int):
    r = requests.get(URL.format(channel_id), headers=HEADERS, timeout=20)
    r.raise_for_status()
    name, programs = parse_page(r.text)
    con.execute("INSERT OR REPLACE INTO channels VALUES (?, ?)", (channel_id, name))
    for p in programs:
        con.execute(
            "INSERT OR REPLACE INTO programs VALUES (?,?,?,?,?,?,?)",
            (channel_id, p["start"].isoformat(), p["end"].isoformat(),
             p["title"], p["kind"], p["year"], p["work_id"]),
        )
    con.commit()
    return name, len(programs)


def discover_channels():
    r = requests.get("https://elcinema.com/tvguide/", headers=HEADERS, timeout=20)
    r.raise_for_status()
    ids = sorted({int(x) for x in re.findall(r"/tvguide/(\d+)", r.text)})
    return ids


def refresh_once(force=False):
    """يسحب القنوات لو البيانات قديمة أو مش موجودة. بيرجّع true لو سحب."""
    con = db()
    try:
        row = con.execute("SELECT v FROM meta WHERE k='last_scrape'").fetchone()
        stale = row is None or (
            cairo_now() - dt.datetime.fromisoformat(row["v"])
        ) > dt.timedelta(hours=REFRESH_HOURS)
        if not (stale or force):
            return False
        ok = 0
        for cid in CHANNELS:
            try:
                scrape_channel(con, cid)
                ok += 1
            except Exception as e:  # قناة فشلت متوقفش الباقي
                print("scrape failed", cid, repr(e), flush=True)
            time.sleep(DELAY_SECONDS)
        if ok:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('last_scrape', ?)",
                        (cairo_now().isoformat(),))
            con.commit()
        return ok > 0
    finally:
        con.close()


def refresh_loop():
    # على الاستضافات المجانية القرص مش دايم، فبنسحب أول ما السيرفر يقوم لو الداتا فاضية
    while True:
        try:
            refresh_once()
        except Exception as e:
            print("refresh error", repr(e), flush=True)
        time.sleep(600)


# ---------------- الـ API ----------------
def make_app():
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    @asynccontextmanager
    async def lifespan(_app):
        threading.Thread(target=refresh_loop, daemon=True).start()
        yield

    api = FastAPI(title="TV Guide", lifespan=lifespan)

    @api.get("/channels")
    def channels():
        with db() as con:
            return [dict(r) for r in con.execute("SELECT * FROM channels ORDER BY name")]

    @api.get("/channels/{channel_id}/programs")
    def programs(channel_id: int, date: str | None = None):
        day = dt.date.fromisoformat(date) if date else cairo_now().date()
        lo = dt.datetime.combine(day, dt.time()).isoformat()
        hi = dt.datetime.combine(day + dt.timedelta(days=1), dt.time()).isoformat()
        with db() as con:
            rows = con.execute(
                "SELECT * FROM programs WHERE channel_id=? AND start>=? AND start<? ORDER BY start",
                (channel_id, lo, hi),
            )
            return [dict(r) for r in rows]

    @api.get("/now")
    def now():
        n = cairo_now().isoformat()
        with db() as con:
            rows = con.execute(
                """SELECT p.*, c.name AS channel_name FROM programs p
                   JOIN channels c ON c.id = p.channel_id
                   WHERE p.start <= ? AND p.end > ? ORDER BY c.name""",
                (n, n),
            )
            return [dict(r) for r in rows]

    # الواجهة: بتتقدم من نفس السيرفر. الملفات ممكن تكون جنب السكريبت أو في مجلد web
    here = os.path.dirname(os.path.abspath(__file__))
    web = os.path.join(here, "web")
    web_dir = web if os.path.isdir(web) else here
    files = {
        "/": ("index.html", "text/html"),
        "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
        "/sw.js": ("sw.js", "application/javascript"),
        "/icon-192.png": ("icon-192.png", "image/png"),
        "/icon-512.png": ("icon-512.png", "image/png"),
    }
    from fastapi.responses import FileResponse

    def add_file_route(url, name, mime):
        path = os.path.join(web_dir, name)

        @api.get(url, include_in_schema=False)
        def _serve():
            return FileResponse(path, media_type=mime)

    for url, (name, mime) in files.items():
        if os.path.isfile(os.path.join(web_dir, name)):
            add_file_route(url, name, mime)

    return api


try:
    app = make_app()
except ImportError:  # fastapi مش متسطب - الـ scraper لسه شغال
    app = None


# ---------------- سطر الأوامر ----------------
if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "dump" and args:
        r = requests.get(URL.format(args[0]), headers=HEADERS, timeout=20)
        open(f"page_{args[0]}.html", "w", encoding="utf-8").write(r.text)
        print(f"اتحفظ page_{args[0]}.html")
    elif cmd == "scrape" and args:
        con = db()
        for cid in args:
            name, n = scrape_channel(con, int(cid))
            print(f"{cid} {name}: {n} برنامج")
            time.sleep(DELAY_SECONDS)
    elif cmd == "discover":
        print(discover_channels())
    else:
        print(__doc__)

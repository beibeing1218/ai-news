#!/usr/bin/env python3
"""AI 新聞看板：抓國外權威媒體 RSS，用免費的 Google 翻譯轉成中文，產生本機網頁。
全程不呼叫任何 AI 模型，不消耗 token。只用 Python 標準庫，不需要 pip install。

用法：
  python ai_news.py build   更新資料並產生網頁（每日排程用）
  python ai_news.py open    開啟看板（必要時在背景啟動本機伺服器，網頁上的「立即更新」才能用）
  python ai_news.py serve   只啟動本機伺服器
"""
import gzip
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
TEMPLATE = BASE / "template.html"
OUT_HTML = BASE / "ai-news.html"      # 可直接雙擊開啟的靜態版
NEWS_JSON = DATA_DIR / "news.json"
# 雲端（GitHub Actions）的快取會存回 repo；本機用另一個不進版控的檔，兩邊才不會互相衝突
CACHE = DATA_DIR / ("translations.json" if os.environ.get("GITHUB_ACTIONS") else "translations.local.json")
LOG = DATA_DIR / "log.txt"

PORT = 8765
DAYS = 7            # 保留幾天內的新聞
STALE_HOURS = 3     # 開啟看板時，資料超過幾小時就自動更新
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AINewsBoard/1.0"}
ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"


def gnews(query):
    """Google News RSS 搜尋；用 site: 限定只收某家媒體。"""
    q = urllib.parse.quote(f"{query} when:{DAYS}d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


AI_Q = '(OpenAI OR Anthropic OR Claude OR Gemini OR DeepMind OR ChatGPT OR "artificial intelligence")'

# (顯示名稱, 類別, RSS 網址)。類別：official 官方、wire 國際財經媒體、tech 科技媒體
FEEDS = [
    ("OpenAI", "official", "https://openai.com/news/rss.xml"),
    ("Anthropic", "official", gnews("site:anthropic.com")),
    ("Google AI Blog", "official", "https://blog.google/technology/ai/rss/"),
    ("Google DeepMind", "official", "https://deepmind.google/blog/rss.xml"),
    ("Reuters", "wire", gnews(f"site:reuters.com {AI_Q}")),
    ("Bloomberg", "wire", gnews(f"site:bloomberg.com {AI_Q}")),
    ("Financial Times", "wire", gnews(f"site:ft.com {AI_Q}")),
    ("The Wall Street Journal", "wire", gnews(f"site:wsj.com {AI_Q}")),
    ("The Verge", "tech", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("TechCrunch", "tech", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("Ars Technica", "tech", "https://arstechnica.com/ai/feed/"),
    ("Wired", "tech", "https://www.wired.com/feed/tag/ai/latest/rss"),
    ("MIT Technology Review", "tech", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
]

# 官方來源一律標上自家公司
OFFICIAL_COMPANY = {"OpenAI": "OpenAI", "Anthropic": "Anthropic",
                    "Google AI Blog": "Google", "Google DeepMind": "Google"}

COMPANIES = {
    "OpenAI": r"openai|chatgpt|gpt-\d[\w.]*|sora|sam altman",
    "Anthropic": r"anthropic|claude|dario amodei",
    "Google": r"google|gemini|deepmind|alphabet",
    "Meta": r"meta|llama|zuckerberg",
    "Microsoft": r"microsoft|copilot|nadella",
    "NVIDIA": r"nvidia|jensen huang",
    "xAI": r"xai|grok",
}
COMPANY_RE = {k: re.compile(rf"\b(?:{v})\b", re.I) for k, v in COMPANIES.items()}
KIND_RANK = {"official": 0, "wire": 1, "tech": 2}

STOP = set("""the a an and or of to in on for with at by from as is are was were be been it its this
that these those how why what who will can could new says said after over into about than more up out
not has have had but his her their our your you we they he she ai just now may amid via all one two
get gets use using make makes first report reports""".split())

# 公司名與常見詞：兩則標題只共用這些字時，不算同一事件
GENERIC = set("""openai anthropic google gemini claude chatgpt deepmind meta microsoft nvidia xai grok
alphabet model agent startup company billion million tech""".split())

# Google 翻譯常把品牌名直譯，這裡改回原名
ZH_FIX = {"人擇": "Anthropic", "克勞德": "Claude", "雙子座": "Gemini", "谷歌": "Google", "開放人工智慧": "OpenAI"}

build_lock = threading.Lock()


def log(msg):
    DATA_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    lines = LOG.read_text(encoding="utf-8").splitlines() if LOG.exists() else []
    lines = (lines + [line])[-300:]
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    if body[:2] == b"\x1f\x8b":        # 有些伺服器（如 DeepMind）會送 gzip 卻不標示
        body = gzip.decompress(body)
    return body


# ---------- 抓取與解析 ----------

def clean(s, limit=None):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(html.unescape(s))
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def child_text(el, *names):
    for n in names:
        x = el.find(n)
        if x is not None:
            if x.text and x.text.strip():
                return x.text.strip()
            if x.get("href"):
                return x.get("href")
    return ""


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fetch_feed(feed, cutoff):
    name, kind, url = feed
    for attempt in range(2):          # 偶爾會拿到空白或壞掉的回應，重試一次
        try:
            root = ET.fromstring(http_get(url))
            break
        except Exception:
            if attempt:
                raise
            time.sleep(3)
    entries = root.findall(".//item") or root.findall(f".//{ATOM}entry")
    is_gnews = "news.google.com" in url
    out = []
    for it in entries:
        title = clean(child_text(it, "title", f"{ATOM}title"))
        link = child_text(it, "link", f"{ATOM}link")
        date = parse_date(child_text(it, "pubDate", f"{ATOM}published", f"{ATOM}updated", f"{DC}date"))
        if not title or not link.startswith("http") or not date or date < cutoff:
            continue
        summary = clean(child_text(it, "description", f"{ATOM}summary", f"{ATOM}content"), 300)
        if is_gnews:
            src = clean(child_text(it, "source"))
            if src and title.endswith(f" - {src}"):
                title = title[: -len(src) - 3]              # 去掉結尾的「 - Reuters」
            title = re.sub(r"^(Exclusive|EXCLUSIVE)\s*[|:-]\s*", "", title)
            summary = ""                                   # Google News 的描述只是連結，沒有內容
        text = f"{title} {summary}"
        companies = {c for c, rx in COMPANY_RE.items() if rx.search(text)}
        if name in OFFICIAL_COMPANY:
            companies.add(OFFICIAL_COMPANY[name])
        out.append({"title": title, "summary": summary, "link": link, "source": name,
                    "kind": kind, "date": date, "companies": companies})
    return out


# ---------- 同一事件歸併（多家報導 = 熱度） ----------

def tokens(title):
    words = re.findall(r"[a-z0-9]+(?:[-.][a-z0-9]+)*", title.lower())
    out = set()
    for w in words:
        if len(w) < 3 or w in STOP:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def similar(a, b):
    shared = a & b
    if len(shared - GENERIC) < 2:
        return False
    j = len(shared) / len(a | b)
    return (len(shared) >= 3 and j >= 0.25) or j >= 0.5


def primary_company(items):
    """一則事件的主角公司：官方來源直接算自家；否則看標題裡最先出現的公司。"""
    lead = items[0]
    if lead["source"] in OFFICIAL_COMPANY:
        return OFFICIAL_COMPANY[lead["source"]]
    for text in [m["title"] for m in items] + [m["summary"] for m in items]:
        hits = [(m.start(), c) for c, rx in COMPANY_RE.items() if (m := rx.search(text))]
        if hits:
            return min(hits)[1]
    return None


def cluster(items):
    items = sorted(items, key=lambda x: x["date"], reverse=True)
    clusters = []
    for it in items:
        it["_tok"] = tokens(it["title"])
        home = None
        for c in clusters:
            if any(abs((m["date"] - it["date"]).days) <= 3 and similar(m["_tok"], it["_tok"]) for m in c):
                home = c
                break
        if home is None:
            clusters.append([it])
        else:
            home.append(it)
    out = []
    for c in clusters:
        # 主標題優先取官方，其次國際財經媒體，再來科技媒體
        c.sort(key=lambda x: (KIND_RANK[x["kind"]], -x["date"].timestamp()))
        out.append({
            "items": c,
            "companies": sorted(set().union(*(m["companies"] for m in c))),
            "primary": primary_company(c),
            "n_sources": len({m["source"] for m in c}),
            "date": max(m["date"] for m in c),
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


# ---------- 免費翻譯（Google 翻譯網頁端點，不需金鑰、不耗 token） ----------

def translate(s):
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-TW&dt=t&q="
           + urllib.parse.quote(s))
    data = json.loads(http_get(url, timeout=20))
    return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()


def fix_zh(s):
    for a, b in ZH_FIX.items():
        s = s.replace(a, b)
    return s


def translate_all(strings):
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    todo = [s for s in strings if s not in cache]
    failures = 0

    def work(s):
        nonlocal failures
        if failures >= 5:          # 連續被擋就先停，下次更新再補翻
            return
        try:
            cache[s] = translate(s)
        except Exception:
            failures += 1

    if todo:
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(work, todo))
    kept = {s: cache[s] for s in strings if s in cache}   # 只保留目前用得到的
    write_atomic(CACHE, json.dumps(kept, ensure_ascii=False))
    return kept, len(todo), failures


# ---------- 產生資料與網頁 ----------

def build():
    with build_lock:
        t0 = time.time()
        DATA_DIR.mkdir(exist_ok=True)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=DAYS)
        items, status = [], []

        def run(feed):
            try:
                return feed, fetch_feed(feed, cutoff), None
            except Exception as e:
                return feed, [], f"{type(e).__name__}: {e}"

        with ThreadPoolExecutor(8) as ex:
            for (name, kind, _), got, err in ex.map(run, FEEDS):
                status.append({"name": name, "kind": kind, "count": len(got), "error": err})
                items.extend(got)
                if err:
                    log(f"[來源失敗] {name}: {err}")

        seen, uniq = set(), []
        for it in sorted(items, key=lambda x: KIND_RANK[x["kind"]]):
            key = (it["link"], re.sub(r"\W+", "", it["title"].lower()))
            if key[0] in seen or key[1] in seen:
                continue
            seen.update(key)
            uniq.append(it)

        clusters = cluster(uniq)
        strings = sorted({s for it in uniq for s in (it["title"], it["summary"]) if s})
        tr, n_new, n_fail = translate_all(strings)

        data = {
            "generated": now.isoformat(),
            "days": DAYS,
            "feeds": status,
            "clusters": [{
                "companies": c["companies"],
                "primary": c["primary"],
                "n_sources": c["n_sources"],
                "date": c["date"].isoformat(),
                "items": [{
                    "title": m["title"], "title_zh": fix_zh(tr.get(m["title"], "")),
                    "summary": m["summary"], "summary_zh": fix_zh(tr.get(m["summary"], "")),
                    "link": m["link"], "source": m["source"], "kind": m["kind"],
                    "date": m["date"].isoformat(),
                } for m in c["items"]],
            } for c in clusters],
        }
        write_atomic(NEWS_JSON, json.dumps(data, ensure_ascii=False))
        write_atomic(OUT_HTML, render(data))
        log(f"更新完成：{len(uniq)} 則新聞、{len(clusters)} 個事件，新翻譯 {n_new - n_fail} 條"
            f"{f'（{n_fail} 條失敗）' if n_fail else ''}，耗時 {time.time() - t0:.0f} 秒")
        return data


def render(data):
    tpl = TEMPLATE.read_text(encoding="utf-8")
    js = json.dumps(data, ensure_ascii=False).replace("</", "<\\/") if data else "null"
    # 雲端版（GitHub Actions）會設定 AI_NEWS_RUN_URL，讓「立即更新」連到手動執行頁
    cfg = json.dumps({"staleHours": STALE_HOURS, "companies": list(COMPANIES),
                      "runUrl": os.environ.get("AI_NEWS_RUN_URL", "")})
    return tpl.replace("/*__DATA__*/null", js).replace("/*__CFG__*/{}", cfg)


# ---------- 本機伺服器（只接受本機連線，讓「立即更新」按鈕能運作） ----------

ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return self._send(403, b"forbidden", "text/plain")
        path = self.path.split("?")[0]
        if path == "/ping":
            return self._send(200, b"ai-news", "text/plain")
        if path in ("/", "/index.html"):
            data = json.loads(NEWS_JSON.read_text(encoding="utf-8")) if NEWS_JSON.exists() else None
            return self._send(200, render(data).encode("utf-8"), "text/html; charset=utf-8")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # 自訂標頭會觸發瀏覽器 CORS 預檢，其他網站因此無法代你按「更新」
        if (self.path != "/refresh" or self.headers.get("Host") not in ALLOWED_HOSTS
                or self.headers.get("X-AI-News") != "1"):
            return self._send(403, b"forbidden", "text/plain")
        try:
            build()
            self._send(200, b'{"ok":true}', "application/json")
        except Exception as e:
            log(f"[更新失敗] {e}")
            self._send(500, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")

    def log_message(self, *args):
        pass


def server_running():
    try:
        return http_get(f"http://127.0.0.1:{PORT}/ping", timeout=2) == b"ai-news"
    except Exception:
        return False


def serve():
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def open_board():
    if not server_running():
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pyw if pyw.exists() else sys.executable)
        flags = 0x00000008 | 0x00000200 | 0x08000000   # 背景執行、不開黑色視窗
        subprocess.Popen([exe, str(Path(__file__).resolve()), "serve"], creationflags=flags, close_fds=True)
        for _ in range(40):
            if server_running():
                break
            time.sleep(0.25)
    webbrowser.open(f"http://127.0.0.1:{PORT}/")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "open"
    {"build": build, "serve": serve, "open": open_board}.get(cmd, open_board)()

"""蒐集層：RSS、Google 新聞搜尋、Tavily 搜尋，加上關鍵字初篩與全文擷取。

任何單一來源失敗都只記錄、不中斷；每個來源的抓取數量會回報，方便發現失效來源。
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import quote_plus, urlsplit, urlunsplit

import feedparser
import httpx

from .config import env
from .schema import RawArticle

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; DisplayIntelBot/0.1; market research)"
TIMEOUT = httpx.Timeout(20.0)


# ---------- 工具函式 ----------

class _TextExtractor(HTMLParser):
    """從 HTML 擷取段落文字（不需額外套件）。"""

    SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript"}
    KEEP = {"p", "li", "h1", "h2", "h3", "td", "th"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
        self._keep = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.KEEP:
            self._keep += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.KEEP and self._keep:
            self._keep -= 1
            self.parts.append("\n")

    def handle_data(self, data):
        if self._keep and not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:  # 壞掉的 HTML 不影響流程
        pass
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = "&".join(q for q in parts.query.split("&") if q and not q.startswith(("utm_", "ref=", "fbclid")))
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def is_weekly(cfg: dict) -> bool:
    return (cfg.get("schedule") or {}).get("cadence", "daily") == "weekly"


# ---------- 初篩 ----------

def classify(article: RawArticle, keywords: dict) -> list[str]:
    """關鍵字初篩：回傳命中的主題，空清單代表不送 AI。"""
    hay = f"{article.title} {article.summary}".lower()

    def hit(group: str) -> bool:
        return any(k.lower() in hay for k in keywords.get(group, []))

    topics = []
    display = hit("display")
    if hit("device") and display:
        topics.append("panel")
    if hit("platform_vendor") and display:
        topics.append("platform")
    if hit("panel_maker") and (hit("fab") or display):
        topics.append("fab")
    return topics


# ---------- 各類來源 ----------

_HOST_LOCKS: dict[str, "threading.Lock"] = {}
_SERIAL_HOSTS = ("reddit.com",)  # 這些網站同時請求會被限流（429），改成依序抓


def _fetch_feed(client: httpx.Client, url: str) -> list:
    import threading
    import time
    from urllib.parse import urlsplit
    host = urlsplit(url).netloc.lower()
    key = next((h for h in _SERIAL_HOSTS if host.endswith(h)), None)
    lock = _HOST_LOCKS.setdefault(key, threading.Lock()) if key else None
    if lock:
        lock.acquire()
    try:
        for attempt in range(2):
            r = client.get(url)
            if r.status_code == 429 and attempt == 0:
                time.sleep(min(30.0, float(r.headers.get("retry-after", 10) or 10)))
                continue
            r.raise_for_status()
            if lock:
                time.sleep(3)  # 同網站兩次請求之間稍微間隔
            return feedparser.parse(r.content).entries
        return []
    finally:
        if lock:
            lock.release()


def _rss_source(client, src: dict, since: datetime) -> list[RawArticle]:
    out = []
    for e in _fetch_feed(client, src["url"]):
        t = _entry_time(e)
        if t and t < since:
            continue
        summary = strip_tags(e.get("summary", ""))
        content = ""
        if e.get("content"):
            content = html_to_text(e["content"][0].get("value", ""))
        out.append(RawArticle(
            url=canonical_url(e.get("link", "")), title=strip_tags(e.get("title", "")),
            source=src["name"], source_type=src.get("type", "media"), lang=src.get("lang", "en"),
            published=t, summary=summary[:2000], content=content,
            content_level="full" if len(content) > 800 else "snippet",
        ))
    return out


def google_news_urls(cfg: dict) -> list[dict]:
    gn = cfg["sources"].get("google_news") or {}
    window = (cfg.get("run") or {}).get("news_window") or ("7d" if is_weekly(cfg) else "1d")
    srcs = []
    for loc in gn.get("locales", []):
        for q in gn.get("queries", {}).get(loc["lang"], []):
            url = (f"https://news.google.com/rss/search?q={quote_plus(q + ' when:' + window)}"
                   f"&hl={loc['hl']}&gl={loc['gl']}&ceid={loc['ceid']}")
            srcs.append({"name": f"Google 新聞（{loc['lang']}）", "url": url, "type": "aggregator", "lang": loc["lang"]})
    return srcs


def _tavily_date(raw) -> datetime | None:
    from email.utils import parsedate_to_datetime
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw[:4].isdigit() else parsedate_to_datetime(raw)
    except (ValueError, TypeError):
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _too_old(url: str, published: datetime | None, cfg: dict) -> bool:
    """新聞搜尋只收近期文章：有發布日期就看日期（超過回看天數＋3 天算舊），沒有就看網址裡的年份。"""
    now = datetime.now(timezone.utc)
    hours = (cfg.get("run") or {}).get("lookback_hours") or 170
    if published:
        return published < now - timedelta(hours=hours) - timedelta(days=3)
    m = re.search(r"/(20\d\d)/", url)
    return bool(m and int(m.group(1)) < now.year - 1 + (now.month > 2))


def _tavily(client, cfg: dict) -> list[RawArticle]:
    key = env("TAVILY_API_KEY")
    tv = cfg["sources"].get("tavily") or {}
    run = cfg.get("run") or {}
    queries = run.get("tavily_queries")
    if queries is None:
        queries = tv.get("queries", [])[: tv.get("max_queries_per_run", 20)]
    if not key or not queries:
        return []
    out = []
    time_range = run.get("tavily_time_range") or ("week" if is_weekly(cfg) else "day")
    window: dict = {"time_range": time_range}
    if run.get("tavily_days"):
        start = (datetime.now(timezone.utc) - timedelta(days=run["tavily_days"])).strftime("%Y-%m-%d")
        window = {"start_date": start}
    for q in queries:
        run["tavily_used"] = run.get("tavily_used", 0) + 1
        try:
            r = client.post("https://api.tavily.com/search",
                            headers={"Authorization": f"Bearer {key}"},
                            json={"query": q, "topic": "news", **window, "search_depth": "basic",
                                  "max_results": tv.get("max_results", 10), "include_raw_content": "text"})
            r.raise_for_status()
            for item in r.json().get("results", []):
                published = _tavily_date(item.get("published_date"))
                if _too_old(item["url"], published, cfg):
                    run["tavily_skipped_old"] = run.get("tavily_skipped_old", 0) + 1
                    continue
                raw = item.get("raw_content") or ""
                out.append(RawArticle(
                    url=canonical_url(item["url"]), title=item.get("title", ""),
                    source=urlsplit(item["url"]).netloc.removeprefix("www.") or "Tavily 搜尋",
                    source_type="media", lang="en", summary=item.get("content", "")[:2000], published=published,
                    content=raw, content_level="full" if len(raw) > 800 else "snippet",
                ))
        except Exception as exc:
            log.warning("Tavily 查詢失敗 %s：%s", q, exc)
    return out


def _fetch_full_text(client, article: RawArticle) -> RawArticle:
    """補抓全文。Google 新聞的連結是轉址頁，抓不到內文，保留摘要即可。"""
    if article.content_level == "full" or "news.google.com" in article.url:
        return article
    try:
        r = client.get(article.url, follow_redirects=True)
        if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
            text = html_to_text(r.text)
            if len(text) > 500:
                article.content = text
                article.content_level = "full"
    except Exception as exc:
        log.debug("全文擷取失敗 %s：%s", article.url, exc)
    return article


# ---------- 對外介面 ----------

def all_sources(cfg: dict) -> list[dict]:
    return list(cfg["sources"].get("rss", [])) + google_news_urls(cfg)


def collect(cfg: dict, now: datetime | None = None, errors: dict | None = None) -> tuple[list[RawArticle], dict[str, int]]:
    """抓取所有來源、初篩、去除重複網址、補抓全文。回傳（文章, 各來源抓取數）。

    errors：傳入 dict 時，會填入「來源名稱 → 失敗原因」，存進執行紀錄方便排查。
    """
    errors = {} if errors is None else errors
    now = now or datetime.now(timezone.utc)
    hours = (cfg.get("run") or {}).get("lookback_hours") or cfg["schedule"].get("lookback_hours", 26)
    since = now - timedelta(hours=hours)
    stats: dict[str, int] = {}
    articles: list[RawArticle] = []

    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=True) as client:
        def run(src):
            try:
                return src, _rss_source(client, src, since), None
            except Exception as exc:
                return src, [], exc

        with ThreadPoolExecutor(max_workers=8) as pool:
            for src, items, exc in pool.map(run, all_sources(cfg)):
                stats[src["name"]] = stats.get(src["name"], 0) + len(items)
                if exc:
                    log.warning("來源失敗 %s：%s", src["name"], exc)
                    errors[src["name"]] = f"{type(exc).__name__}：{str(exc)[:160]}"
                articles.extend(items)

        tav = _tavily(client, cfg)
        stats["Tavily 搜尋"] = len(tav)
        articles.extend(tav)

        # 網址去重 + 初篩
        seen, kept = set(), []
        for a in articles:
            if not a.url or a.url in seen:
                continue
            seen.add(a.url)
            a.topics = classify(a, cfg["keywords"])
            if a.topics:
                kept.append(a)

        with ThreadPoolExecutor(max_workers=8) as pool:
            kept = list(pool.map(lambda a: _fetch_full_text(client, a), kept))

    log.info("蒐集完成：原始 %d 篇，初篩後 %d 篇", len(articles), len(kept))
    return kept, stats


def check_sources(cfg: dict) -> list[tuple[str, str, str]]:
    """逐一測試來源可否連線，回傳 (名稱, 狀態, 說明)。"""
    results = []
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=True) as client:
        for src in all_sources(cfg):
            try:
                n = len(_fetch_feed(client, src["url"]))
                results.append((src["name"], "OK" if n else "空的", f"{n} 則"))
            except Exception as exc:
                results.append((src["name"], "失敗", str(exc)[:120]))
    return results

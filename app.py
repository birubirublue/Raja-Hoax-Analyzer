"""HoaxRaja - Analisa Hoax Indonesia"""
import streamlit as st
import os
import json
import time
import re
import urllib.parse
import requests
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError
from dotenv import load_dotenv

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# Tentukan parser BeautifulSoup (lxml lebih cepat, fallback html.parser jika tidak ada)
_BS4_PARSER = "lxml"
try:
    BeautifulSoup("<html></html>", "lxml")
except Exception:
    _BS4_PARSER = "html.parser"

# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("hoaxraja")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


# ============================================================
# CUSTOM EXCEPTIONS
# ============================================================

class HoaxAnalysisError(Exception):
    """Base error untuk analisis hoax."""


class GeminiAPIError(HoaxAnalysisError):
    """Error dari Google Gemini API."""


class InvalidResponseError(HoaxAnalysisError):
    """Response Gemini tidak bisa diparse."""


class ScraperError(HoaxAnalysisError):
    """Error saat scraping TurnBackHoax.id."""


# ============================================================
# CONSTANTS
# ============================================================

HISTORY_DIR = Path.home() / ".hoaxraja"
HISTORY_FILE = HISTORY_DIR / "history.json"
MAX_HISTORY_ITEMS = 50
HISTORY_DISPLAY_LIMIT = 10
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB - safety cap untuk scraping
SCRAPER_RETRIES = 3
SCRAPER_BACKOFF = 1.5  # detik

_HISTORY_PERSISTENCE_ENABLED = True
try:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    # Test write permission
    test_file = HISTORY_DIR / ".write_test"
    test_file.write_text("test", encoding="utf-8")
    test_file.unlink()
except (OSError, PermissionError):
    # Streamlit Cloud & server lain kadang filesystem read-only di HOME
    _HISTORY_PERSISTENCE_ENABLED = False
    logger.info(
        "Persistent history disabled (filesystem read-only). "
        "History will be session-only."
    )

if not _HISTORY_PERSISTENCE_ENABLED:
    HISTORY_FILE = None  # Disable file ops below

st.set_page_config(
    page_title="Analisa Hoax Indonesia | HoaxRaja",
    page_icon="https://img.icons8.com/color/96/search--v1.png",
    layout="wide",
    initial_sidebar_state="collapsed",
)



# ============================================================
# HELPER FUNCTIONS
# ============================================================

def get_status(score):
    if score <= 30:
        return "success", "AMAN"
    elif score <= 70:
        return "warning", "MENCURIGAKAN"
    else:
        return "error", "HOAX"


def _load_history_from_disk():
    """Load history dari file JSON. Return list kosong jika file belum ada/corrupt,
    atau jika persistence disabled (Streamlit Cloud free tier)."""
    if not _HISTORY_PERSISTENCE_ENABLED or HISTORY_FILE is None:
        return []
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Gagal load history file: %s", e)
    return []


def _save_history_to_disk(history_list):
    """Simpan history ke file JSON. Atomic write untuk cegah corruption.
    Silent no-op jika persistence disabled."""
    if not _HISTORY_PERSISTENCE_ENABLED or HISTORY_FILE is None:
        return
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        trimmed = history_list[-MAX_HISTORY_ITEMS:]
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(trimmed, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(HISTORY_FILE)
    except OSError as e:
        logger.warning("Gagal save history file: %s", e)


def init_history():
    """Inisialisasi session_state.history dari disk jika belum ada."""
    if "history" not in st.session_state:
        st.session_state.history = _load_history_from_disk()
    if "history_loaded_at" not in st.session_state:
        st.session_state.history_loaded_at = datetime.now().isoformat()


def add_history_entry(teks, hasil, status_label):
    """Tambah entry ke history (session + disk)."""
    if "history" not in st.session_state:
        init_history()
    entry = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "waktu": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "teks": teks[:500],
        "skor": int(hasil.probabilitas_hoax),
        "status": status_label,
        "analisis": hasil.analisis_bahasa,
        "kesimpulan": hasil.kesimpulan,
    }
    st.session_state.history.append(entry)
    _save_history_to_disk(st.session_state.history)
    logger.info("History entry added: skor=%d status=%s", entry["skor"], entry["status"])


def _build_scraped_context(teks, scraped_articles):
    if not scraped_articles:
        return teks
    parts = [teks, '']
    parts.append('[BERITA TERKINI - GUNAKAN SEBAGAI FAKTA]:')
    for i, art in enumerate(scraped_articles[:5], 1):
        tag = art.get('source_tag', 'Media')
        title_a = art.get('article_title', art.get('title', ''))
        date_a = art.get('article_date', '')
        body_a = art.get('article_body', '')
        parts.append('')
        parts.append('[Berita ' + str(i) + ' - ' + tag + ']')
        if title_a: parts.append('Judul: ' + title_a)
        if date_a: parts.append('Tanggal: ' + date_a)
        if body_a: parts.append('Isi: ' + body_a[:2000])
        elif art.get('title'): parts.append('Judul: ' + art.get('title', ''))
    parts.append('')
    parts.append('[ANALISIS]:')
    parts.append('1. CLAIM KONTRADIKSI berita terkini = HOAX (skor>=71).')
    parts.append('2. CLAIM DIDUKUNG berita = AMAN (skor<=40).')
    parts.append('3. TIDAK ADA berita = skor 50-65 (mencurigakan).')
    return chr(10).join(parts)



def clear_history():
    """Hapus semua history (session + disk jika persistence enabled)."""
    st.session_state.history = []
    if not _HISTORY_PERSISTENCE_ENABLED or HISTORY_FILE is None:
        return
    try:
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
    except OSError as e:
        logger.warning("Gagal hapus history file: %s", e)


# Inisialisasi history saat app start
init_history()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_article_content(url):
    """Ambil judul, tanggal, body dari article URL."""
    if not url or "://" not in url:
        return {}
    try:
        h = {"User-Agent": "Mozilla/5.0", "Accept-Language": "id-ID"}
        r = requests.get(url, headers=h, timeout=8, stream=True)
        if r.status_code != 200:
            return {}
        data = b""
        for c in r.iter_content(4096):
            data += c
            if len(data) > 50000:
                data = data[:50000]
                break
        r.close()
        html = data.decode("utf-8", errors="replace")
    except:
        return {}
    try:
        soup = BeautifulSoup(html, _BS4_PARSER)
    except:
        return {}
    title = ""
    for t in soup.find_all("meta", property="og:title"):
        title = t.get("content", "")
        break
    if not title:
        t = soup.find("title")
        title = t.get_text(strip=True) if t else ""
    date = ""
    for d in soup.find_all("meta", property="article:published_time"):
        date = d.get("content", "")[:10]
        break
    if not date:
        for d in soup.find_all("time"):
            dt = d.get("datetime") or d.get_text(strip=True)
            date = dt[:10]
            if date:
                break
    sels = [{"class": "detail__body"}, {"class": "itp_bodycontent"},
            {"itemprop": "articleBody"}, {"class": "post-content"}]
    texts = []
    for sel in sels:
        el = soup.find("div", sel) or soup.find("article") or soup.find("main")
        if el:
            for p in el.find_all("p"):
                t = p.get_text(strip=True)
                if len(t) > 40 and not any(x in t.lower() for x in ["cookie", "javascript", "subscribe"]):
                    texts.append(t)
            if texts:
                break
    if not texts:
        for p in soup.find_all("p"):
            t = p.get_text(strip=True)
            if 50 < len(t) < 500:
                texts.append(t)
    return {"title": title[:200], "date": date, "body": " ".join(texts)[:3000], "source": url}



def _fetch_turnbackhoax_html(url: str) -> Optional[str]:
    """Fetch HTML dari TurnBackHoax.id dengan retry + backoff + size cap.

    Returns None jika gagal setelah semua retry.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
    }
    last_err = "unknown"
    for attempt in range(1, SCRAPER_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=10, stream=True)
            if resp.status_code != 200:
                logger.warning("TBH fetch non-200 (%s) untuk %s", resp.status_code, url)
                # 4xx tidak perlu retry, 5xx boleh retry
                if 400 <= resp.status_code < 500:
                    return None
                last_err = "HTTP %s" % resp.status_code
            else:
                # Baca dengan size cap untuk keamanan
                content = b""
                for chunk in resp.iter_content(chunk_size=8192):
                    content += chunk
                    if len(content) > MAX_RESPONSE_BYTES:
                        logger.warning("TBH response exceeds %d bytes, truncating", MAX_RESPONSE_BYTES)
                        content = content[:MAX_RESPONSE_BYTES]
                        break
                resp.close()
                return content.decode("utf-8", errors="replace")
        except requests.Timeout:
            last_err = "timeout"
            logger.warning("TBH fetch timeout (attempt %d/%d) untuk %s", attempt, SCRAPER_RETRIES, url)
        except requests.RequestException as e:
            last_err = str(e)
            logger.warning("TBH fetch error (attempt %d/%d): %s", attempt, SCRAPER_RETRIES, e)
        # Exponential backoff
        if attempt < SCRAPER_RETRIES:
            time.sleep(SCRAPER_BACKOFF ** attempt)
    logger.error("TBH fetch gagal setelah %d percobaan: %s", SCRAPER_RETRIES, last_err)
    return None


# Stopwords untuk ekstraksi keyword pencarian
_TBH_STOPWORDS = frozenset({
    "yang", "dan", "dengan", "untuk", "adalah", "ini", "itu",
    "pada", "akan", "sudah", "tidak", "ada", "oleh", "atau", "jika", "maka",
    "juga", "seperti", "dalam", "tersebut", "bisa", "kamu", "saya", "kita",
    "mereka", "apa", "siapa", "mana", "kapan", "mengapa", "bagaimana", "tahun",
    "bulan", "hari", "minggu", "pernah", "belum", "masih",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    # 'di', 'ke', 'dari' dihapus - sering jadi bagian nama/istilah penting
})


def _extract_keywords(text: str, min_len: int = 4) -> list:
    """Ekstrak keyword penting dari teks (skip stopwords)."""
    return [
        w for w in re.findall(r"[a-zA-Z]{%d,}" % min_len, text.lower())
        if w not in _TBH_STOPWORDS
    ]


def _classify_hoax_type(title: str) -> str:
    """Klasifikasi tipe hoax dari judul artikel TurnBackHoax."""
    tl = title.lower()
    if "[salah]" in tl:
        return "SALAH"
    if "[penipuan]" in tl:
        return "PENIPUAN"
    if "[fakta]" in tl:
        return "KLARIFIKASI"
    if "[lebikan]" in tl:
        return "LEBIKAN"
    return ""


@st.cache_data(ttl=300, show_spinner=False)
def search_turnbackhoax(query, max_results=5):
    """Search TurnBackHoax.id dengan validasi relevansi.

    Returns list of dict berisi artikel relevan (relevance >= 30%).
    Returns empty list jika scraping gagal atau query tidak valid.
    """
    if not BS4_AVAILABLE:
        logger.info("BeautifulSoup tidak tersedia, skip TBH search")
        return []
    if not query or len(query.strip()) < 3:
        return []

    keywords = _extract_keywords(query)
    if not keywords:
        return []

    # Gunakan '+' untuk pemisah kata di URL TBH
    search_query = "+".join(keywords[:5])
    search_url = "https://turnbackhoax.id/?s=" + search_query

    html = _fetch_turnbackhoax_html(search_url)
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, _BS4_PARSER)
        # Ambil lebih dari yang dibutuhkan untuk filtering
        cards = soup.select(".news-card-v")[:max_results * 3]

        results = []
        for card in cards:
            try:
                link_tag = card.find("a", href=True)
                if not link_tag:
                    continue
                title = link_tag.get_text(strip=True)
                url = link_tag["href"]
                if not (title and url and "/articles/" in url):
                    continue

                excerpt_tag = card.find("p")
                excerpt = excerpt_tag.get_text(strip=True)[:200] if excerpt_tag else ""

                date_el = card.find("span")
                date = date_el.get_text(strip=True) if date_el else ""

                # Hitung skor relevansi
                title_lower = title.lower()
                excerpt_lower = excerpt.lower()
                matches = sum(
                    1 for kw in keywords
                    if kw in title_lower or kw in excerpt_lower
                )
                relevance = matches / len(keywords) if keywords else 0

                # Filter: minimal 30% keyword match
                if relevance < 0.30:
                    continue

                results.append({
                    "title": title,
                    "url": url,
                    "date": date,
                    "excerpt": excerpt,
                    "hoax_type": _classify_hoax_type(title),
                    "relevance": round(relevance, 2),
                    "matches": matches,
                })
            except (AttributeError, KeyError, TypeError) as e:
                logger.debug("Skip malformed TBH card: %s", e)
                continue

        # Sort by relevance (highest first)
        results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
        logger.info("TBH search '%s' -> %d artikel relevan", keywords[:3], len(results))
        return results[:max_results]
    except Exception as e:
        logger.error("TBH search parse error: %s", e)
        return []


@st.cache_data(ttl=600, show_spinner=False)
def get_latest_hoaxes_tbh(max_results=8):
    """Ambil daftar hoax terbaru dari halaman utama TurnBackHoax.id."""
    if not BS4_AVAILABLE:
        return []
    html = _fetch_turnbackhoax_html("https://turnbackhoax.id/")
    if not html:
        return []
    results = []
    try:
        soup = BeautifulSoup(html, _BS4_PARSER)
        cards = soup.select(".news-card-v")[:max_results]
        seen_urls = set()
        for card in cards:
            try:
                link_tag = card.find("a", href=True)
                if not link_tag:
                    continue
                title = link_tag.get_text(strip=True)
                url = link_tag["href"]
                if title and url and "/articles/" in url and url not in seen_urls:
                    seen_urls.add(url)
                    results.append({"title": title[:150], "url": url, "date": ""})
            except (AttributeError, KeyError, TypeError):
                continue
    except Exception as e:
        logger.error("TBH latest parse error: %s", e)
    return results


# ============================================================
# MULTI-SOURCE NEWS SCRAPER
# ============================================================

def _fetch_html(url: str):
    """Fetch HTML generik (reuse logic dari _fetch_turnbackhoax_html)."""
    return _fetch_turnbackhoax_html(url)


_NEWS_SOURCES = {
    "detik": {"display_name": "Detik.com", "tag": "DETIK", "domain": "detik.com", "priority": 1},
    "cnn": {"display_name": "CNN Indonesia", "tag": "CNN", "domain": "cnnindonesia.com", "priority": 2},
    "antara": {"display_name": "Antara News", "tag": "ANTARA", "domain": "antaranews.com", "priority": 3},
    "liputan6": {"display_name": "Liputan6 (Cek Fakta)", "tag": "LIPUTAN6", "domain": "liputan6.com", "priority": 4},
    "republika": {"display_name": "Republika", "tag": "REPUBLIKA", "domain": "republika.co.id", "priority": 5},
    "suara": {"display_name": "Suara.com", "tag": "SUARA", "domain": "suara.com", "priority": 6},
    "okezone": {"display_name": "Okezone", "tag": "OKEZONE", "domain": "okezone.com", "priority": 7},
}


def _parse_detik(html: str):
    """Parse hasil search Detik.com."""
    results = []
    soup = BeautifulSoup(html, _BS4_PARSER)
    for a in soup.select("article a[href]"):
        try:
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not (title and "detik.com" in href and "/search" not in href and len(title) > 25):
                continue
            results.append({"title": title[:200], "url": href, "excerpt": "", "date": ""})
        except (AttributeError, KeyError, TypeError):
            continue
        if len(results) >= 10:
            break
    return results


def _parse_antara(html: str):
    """Parse hasil search Antara News (URL pattern: /berita/{id}/{slug})."""
    results = []
    soup = BeautifulSoup(html, _BS4_PARSER)
    for a in soup.select("a[href]"):
        try:
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not (title and "/berita/" in href and 25 < len(title) < 250):
                continue
            if "/kategori/" in href or "/topic/" in href:
                continue
            results.append({"title": title[:200], "url": href, "excerpt": "", "date": ""})
        except (AttributeError, KeyError, TypeError):
            continue
        if len(results) >= 10:
            break
    return results


def _parse_liputan6(html: str):
    """Parse hasil search Liputan6 (URL pattern: /cek-fakta/read/{id}/{slug} atau /news/read/...)."""
    results = []
    soup = BeautifulSoup(html, _BS4_PARSER)
    for a in soup.select("a[href]"):
        try:
            href = a.get("href", "")
            title = a.get_text(strip=True)
            is_cek_fakta = "/cek-fakta/" in href
            is_news = "/read/" in href
            if not (title and "liputan6.com" in href and (is_cek_fakta or is_news)
                    and 25 < len(title) < 250):
                continue
            results.append({
                "title": title[:200], "url": href, "excerpt": "", "date": "",
                "is_cek_fakta": is_cek_fakta,
            })
        except (AttributeError, KeyError, TypeError):
            continue
        if len(results) >= 10:
            break
    return results


def _parse_republika(html: str):
    """Parse hasil search Republika (URL pattern: /berita/{id}/{slug})."""
    results = []
    soup = BeautifulSoup(html, _BS4_PARSER)
    for a in soup.select("a[href]"):
        try:
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not (title and "republika.co.id" in href and "/berita/" in href
                    and 25 < len(title) < 250):
                continue
            results.append({"title": title[:200], "url": href, "excerpt": "", "date": ""})
        except (AttributeError, KeyError, TypeError):
            continue
        if len(results) >= 10:
            break
    return results


def _parse_suara(html: str):
    """Parse hasil search Suara.com."""
    results = []
    soup = BeautifulSoup(html, _BS4_PARSER)
    for a in soup.select("a[href]"):
        try:
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not (title and "suara.com" in href and "/search" not in href
                    and 25 < len(title) < 250):
                continue
            if any(seg in href for seg in ["category", "/author/"]):
                continue
            results.append({"title": title[:200], "url": href, "excerpt": "", "date": ""})
        except (AttributeError, KeyError, TypeError):
            continue
        if len(results) >= 10:
            break
    return results


def _parse_okezone(html: str):
    """Parse hasil search Okezone (URL pattern: /read/{year}/{month}/{id}/{slug})."""
    results = []
    soup = BeautifulSoup(html, _BS4_PARSER)
    for a in soup.select("a[href]"):
        try:
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not (title and "okezone.com" in href and "/read/" in href
                    and 25 < len(title) < 250):
                continue
            results.append({"title": title[:200], "url": href, "excerpt": "", "date": ""})
        except (AttributeError, KeyError, TypeError):
            continue
        if len(results) >= 10:
            break
    return results


def _parse_cnn(html: str):
    """Parse hasil search CNN Indonesia."""
    results = []
    soup = BeautifulSoup(html, _BS4_PARSER)
    for a in soup.select("a[href]"):
        try:
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not (title and "cnnindonesia.com" in href and "/search" not in href
                    and 25 < len(title) < 250):
                continue
            if any(seg in href for seg in ["category", "/author/", "/tag/"]):
                continue
            results.append({"title": title[:200], "url": href, "excerpt": "", "date": ""})
        except (AttributeError, KeyError, TypeError):
            continue
        if len(results) >= 10:
            break
    return results



_PARSERS = {
    "detik": _parse_detik,
    "cnn": _parse_cnn,
    "antara": _parse_antara,
    "liputan6": _parse_liputan6,
    "republika": _parse_republika,
    "suara": _parse_suara,
    "okezone": _parse_okezone,
}

_SEARCH_URL_TEMPLATES = {
    "detik": lambda q: "https://www.detik.com/search/searchall?query=" + urllib.parse.quote_plus(q),
    "cnn": lambda q: "https://www.cnnindonesia.com/search/?query=" + urllib.parse.quote_plus(q),
    "antara": lambda q: "https://www.antaranews.com/search/?q=" + urllib.parse.quote_plus(q),
    "liputan6": lambda q: "https://www.liputan6.com/search?q=" + urllib.parse.quote_plus(q),
    "republika": lambda q: "https://www.republika.co.id/search?q=" + urllib.parse.quote_plus(q),
    "suara": lambda q: "https://www.suara.com/search?q=" + urllib.parse.quote_plus(q),
    "okezone": lambda q: "https://search.okezone.com/search?q=" + urllib.parse.quote_plus(q),
}


def _search_single_source(source_key, query, max_results=3):
    """Search SATU sumber berita. Return list of dict atau [] jika gagal."""
    if not BS4_AVAILABLE:
        return []
    if not query or len(query.strip()) < 3:
        return []
    src_info = _NEWS_SOURCES.get(source_key)
    parser = _PARSERS.get(source_key)
    url_fn = _SEARCH_URL_TEMPLATES.get(source_key)
    if not (src_info and parser and url_fn):
        return []
    try:
        url = url_fn(query.strip())
        html = _fetch_html(url)
        if not html:
            return []
        items = parser(html)
        for item in items[:max_results]:
            item["source_key"] = source_key
            item["source_name"] = src_info["display_name"]
            item["source_tag"] = src_info["tag"]
            item["domain"] = src_info["domain"]
        return items[:max_results]
    except Exception as e:
        logger.warning("Search %s gagal: %s", source_key, e)
        return []


@st.cache_data(ttl=300, show_spinner=False)
def search_news_multi_source(query, max_per_source=3, enabled_sources=None):
    """Search berita dari beberapa sumber media Indonesia secara sequential.

    Args:
        query: kata kunci pencarian
        max_per_source: max artikel per sumber
        enabled_sources: list source_key yang aktif (None = semua)

    Returns:
        list of dict (gabungan dari semua sumber, sudah diproses + dedup + sort)
    """
    if not BS4_AVAILABLE:
        return []
    if not query or len(query.strip()) < 3:
        return []
    if enabled_sources is None:
        enabled_sources = list(_NEWS_SOURCES.keys())

    keywords = _extract_keywords(query)
    if not keywords:
        return []

    all_results = []
    for src_key in enabled_sources:
        items = _search_single_source(src_key, query, max_results=max_per_source)
        all_results.extend(items)

    # Filter relevansi - minimal 1 keyword match
    filtered = []
    for item in all_results:
        title_lower = item.get("title", "").lower()
        matches = sum(1 for kw in keywords if kw in title_lower)
        if matches == 0:
            continue
        item["matches"] = matches
        item["relevance"] = round(matches / len(keywords), 2)
        filtered.append(item)

    # Sort: cek_fakta prioritized > matches desc > source_key asc (konsistensi)
    filtered.sort(
        key=lambda x: (
            not x.get("is_cek_fakta", False),
            -int(x.get("matches", 0)),
            _NEWS_SOURCES.get(x.get("source_key", ""), {}).get("priority", 99),
        )
    )

    # Dedup by URL
    seen_urls = set()
    final = []
    for item in filtered:
        url = item.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            final.append(item)

    logger.info(
        "Multi-source search '%s' -> %d artikel dari %d sumber aktif",
        keywords[:3], len(final), len(enabled_sources),
    )
    return final


def retry_api_call(func, max_retries=3, delay=2):
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            err = str(e)
            if attempt < max_retries - 1 and any(x in err for x in ["429", "quota", "500", "503"]):
                time.sleep(delay * (attempt + 1))
                continue
            raise


def generate_share_text(teks, skor, kesimpulan):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    short_teks = teks[:100] + ("..." if len(teks) > 100 else "")
    return "\n".join([
        "HASIL ANALISA HOAX - HoaxRaja",
        "=" * 40,
        "Teks   : " + short_teks,
        "Skor   : " + str(skor) + "%",
        "Status : " + kesimpulan,
        "Waktu  : " + timestamp,
        "=" * 40,
        "Cek di: https://hoaxraja.streamlit.app",
    ])


def validate_teks_input(teks):
    if not teks or not teks.strip():
        return False, "Teks tidak boleh kosong."
    stripped = teks.strip()
    if len(stripped) < 10:
        return False, "Teks terlalu pendek. Minimal 10 karakter."
    if len(stripped) > 10000:
        return False, "Teks terlalu panjang. Maksimal 10.000 karakter."
    # Count words (split by whitespace)
    words = stripped.split()
    if len(words) < 4:
        return False, (
            "Teks terlalu pendek untuk dianalisis. Mohon masukkan klaim lengkap, "
            "contoh: \u0027[Nama Tokoh] dikabarkan meninggal dunia dalam kecelakaan\u0027 "
            "minimal 4 kata. Nama orang saja tanpa konteks klaim tidak bisa dianalisis."
        )
    return True, ""


# ============================================================
# PYDANTIC MODELS
# ============================================================

class HoaxAnalysis(BaseModel):
    probabilitas_hoax: int = Field(..., ge=0, le=100)
    analisis_bahasa: str
    cek_fakta: str
    kesimpulan: str
    ringkasan_verifikasi: str = Field(default="")


# ============================================================
# ============================================================
# PROMPTS (modular, full Indonesian, dengan few-shot examples)
# ============================================================
# Catatan: gunakan .replace() (bukan .format()) untuk placeholder tanggal
# agar JSON braces {} di few-shot tidak konflik dengan format spec.

_PROMPT_PERAN = (
    "Kamu adalah analis disinformasi berpengalaman untuk konten bahasa Indonesia. "
    "Tugasmu membantu masyarakat mengenali berita hoax, misinformasi, dan disinformasi "
    "dengan analisis linguistik yang teliti dan verifikasi fakta yang kredibel."
)

_PROMPT_TUGAS = (
    "TUGAS UTAMA:\n"
    "1. Analisis pola bahasa: deteksi clickbait, klaim absolut tanpa sumber, "
    "fearmongering, dan ketidakjelasan sumber informasi.\n"
    "2. Verifikasi fakta real-time menggunakan Google Search (jika tersedia) "
    "sebelum memberikan skor akhir."
)

_PROMPT_ATURAN_SKOR = (
    "ATURAN PENILAIAN:\n"
    "- Skor 0-30  : Aman / netral / informatif (berita faktual dengan sumber jelas)\n"
    "- Skor 31-70 : Mencurigakan (butuh verifikasi lanjutan ke sumber resmi)\n"
    "- Skor 71-100: Kemungkinan besar Hoax / disinformasi\n"
    "- Kesimpulan HARUS salah satu: 'Aman' | 'Mencurigakan' | 'Hoax' | 'Perlu Konteks Tambahan'\n"
    "- Gunakan Bahasa Indonesia formal dan profesional dalam seluruh respons."
)

_PROMPT_TANGGAL_TEMPLATE = (
    "PEDOMAN PENANGANAN TANGGAL:\n"
    "- Hari ini adalah __CURRENT_DATE__.\n"
    "- Tanggal dalam rentang __DATE_START__ sampai __DATE_END__ WAJIB "
    "dianggap VALID (masa lalu atau sekarang), BUKAN tanggal masa depan.\n"
    "- Jangan tandai berita sebagai hoax HANYA karena tanggal sedikit di masa lalu. "
    "Misal hari ini 16 September 2026, berita tanggal 15 September 2026 = VALID (kemarin).\n"
    "- Hanya anggap anomali tanggal jika tanggal lebih dari 2 tahun ke depan "
    "(misal 2030+) atau tidak logis (misal '13 bulan dalam setahun')."
)

_PROMPT_SUMBER = (
    "PEDOMAN SUMBER BERITA KREDIBEL:\n"
    "- Media Indonesia tepercaya: kompas.com, detik.com, tempo.co, liputan6.com, "
    "cnnindonesia.com, tribunnews.com, republika.co.id, kontan.co.id, sindonews.com, "
    "okezone.com, suara.com, jpnn.com, antaranews.com, beritasatu.com.\n"
    "- Internasional tepercaya: reuters.com, bbc.com, theguardian.com, apnews.com.\n"
    "- Layanan cek fakta: turnbackhoax.id, cekfakta.com, snopes.com.\n"
    "- Jika teks merupakan salinan lengkap artikel media kredibel (memiliki judul, "
    "penulis, editor, tanggal, lokasi, isi berita), BUKAN hoax. Hoax biasanya berupa "
    "klaim singkat tanpa konteks.\n"
    "- Bedakan 'laporan/dakwaan' dengan 'putusan terbukti'. "
    "Berita 'dilaporkan ke polisi' = fakta laporan, bukan fakta terbukti."
)

_PROMPT_KONTEKS = (
    "PEDOMAN KONTEKS MINIMAL:\n"
    "- Jika input HANYA berisi nama orang/tokoh tanpa konteks klaim "
    "(misal: '[Nama Tokoh]'), JANGAN beri skor 0 atau 100. "
    "Tandai kesimpulan sebagai 'Perlu Konteks Tambahan' dengan skor 50-65.\n"
    "- Selalu sarankan user menyertakan klaim lengkap, "
    "contoh: '[Nama Tokoh] dikabarkan meninggal dunia' BUKAN cuma '[Nama Tokoh]'.\n"
    "- Jika teks kurang dari 4 kata, kesimpulan WAJIB 'PERLU KONTEKS TAMBAHAN'."
)

_PROMPT_FEW_SHOT = (
    "CONTOH OUTPUT YANG DIHARAPKAN (3 contoh):\n\n"
    "Contoh 1 - Berita faktual:\n"
    "Input: 'Kompas.com (Jakarta) - Presiden Jokowi mengumumkan kebijakan baru "
    "ekonomi pada Senin (15/9). Penulis: Budi Santoso.'\n"
    "Output: " + chr(123) + "\"probabilitas_hoax\": 10, \"analisis_bahasa\": \"Struktur berita lengkap "
    "dengan sumber jelas (Kompas), penulis bernama, dan tanggal spesifik. Tidak ada "
    "indikasi clickbait atau emosi berlebihan.\", \"cek_fakta\": \"Cocokkan dengan "
    "sumber Kompas asli di kompas.com. Verifikasi bahwa penulis Budi Santoso benar "
    "menulis artikel tersebut.\", \"kesimpulan\": \"Aman\", \"ringkasan_verifikasi\": \"\"" + chr(125) + "\n\n"
    "Contoh 2 - Kemungkinan hoax:\n"
    "Input: 'VIRAL!! Minum air kelapa muda bisa sembuhkan COVID-19 dalam 1 hari. "
    "Bagikan ke 10 grup WA Anda!' (broadcast WA)\n"
    "Output: " + chr(123) + "\"probabilitas_hoax\": 88, \"analisis_bahasa\": \"Pola clickbait sangat "
    "kuat: CAPS LOCK, tanda seru berlebihan, klaim absolut tanpa sumber medis, "
    "dan ajakan viral yang khas disinformasi.\", \"cek_fakta\": \"Cek di WHO, "
    "Kompas, Detik: tidak ada bukti ilmiah air kelapa menyembuhkan COVID-19. "
    "Konsultasikan dengan dokter untuk informasi medis.\", \"kesimpulan\": \"Hoax\", "
    "\"ringkasan_verifikasi\": \"WHO dan Kemenkes RI menyatakan tidak ada obat herbal "
    "yang terbukti menyembuhkan COVID-19 dalam 1 hari.\"" + chr(125) + "\n\n"
    "Contoh 3 - Konteks tidak cukup:\n"
    "Input: '[Nama Tokoh]'\n"
    "Output: " + chr(123) + "\"probabilitas_hoax\": 55, \"analisis_bahasa\": \"Teks hanya berisi nama "
    "tokoh tanpa konteks klaim. Tidak cukup informasi untuk menilai hoax atau bukan.\","
    " \"cek_fakta\": \"Sertakan klaim lengkap, contoh: '[Nama Tokoh] dikabarkan "
    "meninggal dunia'. Baru bisa diverifikasi.\", \"kesimpulan\": \"Perlu Konteks "
    "Tambahan\", \"ringkasan_verifikasi\": \"\"" + chr(125) + "\n"
)

_PROMPT_INSTRUKSI_JSON = (
    "INSTRUKSI OUTPUT:\n"
    "- Respons HARUS dalam format JSON valid sesuai schema yang diberikan.\n"
    "- Setiap field string minimal 20 karakter, kecuali ringkasan_verifikasi yang "
    "boleh kosong string jika tidak ada verifikasi.\n"
    "- Bahasa Indonesia formal, tidak boleh ada emoji atau karakter markdown.\n"
    "- Skor probabilitas_hoax WAJIB bilangan bulat 0-100."
)

SYSTEM_PROMPT_BASE = (
    _PROMPT_PERAN + "\n\n"
    + _PROMPT_TUGAS + "\n\n"
    + _PROMPT_ATURAN_SKOR + "\n\n"
    + _PROMPT_TANGGAL_TEMPLATE + "\n\n"
    + _PROMPT_SUMBER + "\n\n"
    + _PROMPT_KONTEKS + "\n\n"
    + _PROMPT_FEW_SHOT + "\n\n"
    + _PROMPT_INSTRUKSI_JSON
)

SEARCH_INSTRUCTION = (
    "\n\nVERIFIKASI FAKTA VIA GOOGLE SEARCH (GROUNDING - WAJIB DILAKUKAN):\n"
    "Kamu memiliki akses ke Google Search untuk GROUNDING. "
    "Hasil pencarian akan dipakai sebagai BASIS faktual untuk responsmu.\n\n"
    "Langkah-langkah WAJIB:\n"
    "1. Identifikasi klaim faktual utama (nama tokoh, peristiwa, angka, tanggal spesifik).\n"
    "2. Gunakan GROUNDING tool untuk mencari sumber terkini.\n"
    "3. Sumber prioritas: kemenkeu.go.id, kemkes.go.id, setkab.go.id, "
    "kompas.com, detik.com, cnnindonesia.com, turnbackhoax.id.\n"
    "4. Jika hasil pencarian KONTRADIKSI dengan klaim user -> skor >= 71.\n"
    "5. Jika hasil pencarian MENDUKUNG klaim user -> skor <= 40.\n"
    "6. Jika TIDAK ADA hasil pencarian -> skor 50-65 (belum bisa diverifikasi).\n"
    "7. Isi ringkasan_verifikasi dengan temuan grounding (tanggal + sumber).\n"
    "8. Contoh: Sri Mulyani dilantik sebagai Menkeu 22 Oktober 2024 "
    "(Kabinet Prabowo-Gibran), BUKAN Purbaya.\n"
    "9. ERROR FATAL: Knowledge lama tanpa grounding = misinformasi. WAJIB grounding.\n"
)

def build_system_prompt(current_date, date_window_start, date_window_end):
    """Build prompt dengan placeholder tanggal diisi via .replace() (aman untuk JSON)."""
    return (
        SYSTEM_PROMPT_BASE
        .replace("__CURRENT_DATE__", current_date)
        .replace("__DATE_START__", date_window_start)
        .replace("__DATE_END__", date_window_end)
    )



def analisis_hoax(teks, api_key, model_name=None):
    client = genai.Client(api_key=api_key)
    model = model_name or "gemini-3.5-flash"
    # Inject current date context so Gemini doesn't mistake recent dates as "future"
    now = datetime.now()
    current_date = now.strftime("%d %B %Y")  # e.g. "16 September 2026"
    # Date window: 1 year ago to 1 year ahead = all VALID
    date_window_start = now.replace(year=now.year - 1).strftime("%d %B %Y")
    date_window_end = now.replace(day=min(now.day, 28)).replace(month=12).replace(year=now.year + 1).strftime("%d %B %Y")
    system_prompt_filled = build_system_prompt(
        current_date=current_date,
        date_window_start=date_window_start,
        date_window_end=date_window_end,
    )

    prompt = system_prompt_filled
    tools = None

    full_text = _build_scraped_context(teks, scraped_articles)

    def _call():
        return client.models.generate_content(
            model=model, contents=full_text,
            config=types.GenerateContentConfig(
                system_instruction=prompt,
                response_mime_type="application/json",
                response_schema=HoaxAnalysis,
                temperature=0.4,
                max_output_tokens=2048,
                tools=tools,
            ),
        )

    try:
        resp = retry_api_call(_call)
    except Exception as e:
        err_str = str(e)
        # Translate genai errors ke custom exception
        if any(x in err_str for x in ["API key", "401", "403", "unauthenticated", "PERMISSION_DENIED"]):
            raise GeminiAPIError("API Key tidak valid atau tidak punya akses.") from e
        if any(x in err_str.lower() for x in ["429", "quota", "rate limit", "RESOURCE_EXHAUSTED"]):
            raise GeminiAPIError("Rate limit / quota terlampaui.") from e
        if any(x in err_str.lower() for x in ["timeout", "deadline", "DEADLINE_EXCEEDED"]):
            raise GeminiAPIError("Request timeout ke Gemini.") from e
        if any(x in err_str for x in ["not found", "404", "model"]):
            raise GeminiAPIError(
                "Model '" + model + "' tidak ditemukan. Cek nama model di konfigurasi."
            ) from e
        raise GeminiAPIError("Error Gemini API: " + err_str) from e

    raw = ""
    try:
        raw = (resp.text or "").strip()
    except (ValueError, AttributeError):
        try:
            parts = resp.candidates[0].content.parts
            raw = "".join(getattr(p, "text", "") or "" for p in parts).strip()
        except (AttributeError, IndexError, TypeError):
            raw = ""

    if not raw:
        raise InvalidResponseError("Gemini tidak mengembalikan respons.")

    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines[0].startswith("```"): lines = lines[1:]
        if lines and lines[-1].strip() == "```": lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as je:
        logger.warning("JSON parse gagal, coba extract substring. Error: %s", je)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError as je2:
                logger.error("JSON extract tetap gagal: %s", je2)
                raise InvalidResponseError(
                    "Respons Gemini bukan JSON valid. Raw (300 char): " + raw[:300]
                ) from je2
        else:
            raise InvalidResponseError(
                "Respons Gemini tidak mengandung JSON. Raw (300 char): " + raw[:300]
            )

    try:
        hasil = HoaxAnalysis(**data)
    except (ValidationError, TypeError) as ve:
        logger.error("Pydantic validation gagal: %s", ve)
        raise InvalidResponseError(
            "Schema respons Gemini tidak sesuai. Cek field wajib: "
            "probabilitas_hoax, analisis_bahasa, cek_fakta, kesimpulan, "
            "ringkasan_verifikasi."
        ) from ve

    sumber = []
    try:
        if resp.candidates:
            cand = resp.candidates[0]
            grounding = getattr(cand, "grounding_metadata", None)
            if grounding:
                chunks = getattr(grounding, "grounding_chunks", []) or []
                for chunk in chunks:
                    try:
                        web = getattr(chunk, "web", None)
                        if web:
                            uri = getattr(web, "uri", "") or ""
                            title = getattr(web, "title", "") or ""
                            if uri and title:
                                sumber.append({"title": title, "uri": uri})
                    except AttributeError:
                        continue
            else:
                logger.info("Tidak ada grounding metadata (search grounding nonaktif)")
    except (AttributeError, IndexError) as e:
        logger.debug("Error ekstrak grounding: %s", e)
    return hasil, sumber



# ============================================================
# STREAMLIT UI
# ============================================================

st.markdown("""<style>
.main-header { font-size: 2.4rem; font-weight: 800; background: linear-gradient(135deg, #60a5fa 0%, #a78bfa 50%, #f472b6 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; text-align: center; padding: 1.2rem 0 0.8rem 0; margin-bottom: 0; letter-spacing: -0.5px; }
.sub-header { font-size: 1.05rem; color: #94a3b8; text-align: center; margin-bottom: 2rem; font-weight: 400; }
.author-credit { text-align: center; color: #64748b; font-size: 0.85rem; padding: 1.5rem 0 0.5rem 0; margin-top: 2rem; border-top: 1px solid rgba(148,163,184,0.2); }
.tbh-widget { background: linear-gradient(135deg, rgba(245,158,11,0.15) 0%, rgba(217,119,6,0.1) 100%); border-radius: 12px; padding: 1.1rem 1.3rem; margin-bottom: 0.8rem; border: 1px solid rgba(245,158,11,0.4); border-left: 4px solid #f59e0b; transition: transform 0.2s ease; }
.tbh-widget:hover { transform: translateX(4px); }
.tbh-widget a { color: inherit; text-decoration: none; font-weight: 600; }
.tbh-widget a:hover { color: #fbbf24; }
.tbh-badge { display: inline-block; padding: 3px 10px; border-radius: 6px; font-size: 0.72rem; font-weight: 700; margin-bottom: 6px; letter-spacing: 0.5px; text-transform: uppercase; }
.tbh-badge-salah { background: linear-gradient(135deg, #dc2626 0%, #991b1b 100%); color: #fff; box-shadow: 0 0 12px rgba(220,38,38,0.5); }
.tbh-badge-penipuan { background: linear-gradient(135deg, #7c3aed 0%, #5b21b6 100%); color: #fff; box-shadow: 0 0 12px rgba(124,58,237,0.5); }
.tbh-badge-fakta { background: linear-gradient(135deg, #10b981 0%, #047857 100%); color: #fff; box-shadow: 0 0 12px rgba(16,185,129,0.5); }
.tbh-badge-lebikan { background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); color: #fff; }
.tbh-badge-default { background: rgba(100,116,139,0.6); color: #e2e8f0; }
/* News source badges - distinct colors per media */
.news-widget { padding: 12px; margin: 8px 0; background: rgba(15,23,42,0.5); border-radius: 8px; border-left: 3px solid #60a5fa; }
.news-widget a { color: inherit; text-decoration: none; font-weight: 600; }
.news-widget a:hover { color: #fbbf24; }
.source-badge { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.68rem; font-weight: 700; margin-right: 6px; letter-spacing: 0.4px; }
.source-DETIK { background: linear-gradient(135deg, #ef4444 0%, #b91c1c 100%); color: #fff; }
.source-ANTARA { background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); color: #fff; }
.source-LIPUTAN6 { background: linear-gradient(135deg, #06b6d4 0%, #0891b2 100%); color: #fff; }
.source-REPUBLIKA { background: linear-gradient(135deg, #16a34a 0%, #15803d 100%); color: #fff; }
.source-SUARA { background: linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%); color: #fff; }
.source-OKEZONE { background: linear-gradient(135deg, #ec4899 0%, #be185d 100%); color: #fff; }
.cek-fakta-badge { background: linear-gradient(135deg, #fbbf24 0%, #d97706 100%); color: #000; padding: 2px 8px; border-radius: 5px; font-size: 0.68rem; font-weight: 700; margin-right: 6px; box-shadow: 0 0 8px rgba(251,191,36,0.5); }
div[data-testid="stMetricValue"] { font-size: 2.2rem !important; font-weight: 800 !important; }
div[data-testid="stMetricDelta"] { font-size: 0.9rem !important; }
.stTabs [data-baseweb="tab-list"] { gap: 4px; background: transparent; }
.stTabs [data-baseweb="tab"] { background: rgba(148,163,184,0.1); border-radius: 8px 8px 0 0; padding: 10px 20px; color: #94a3b8; font-weight: 500; }
.stTabs [aria-selected="true"] { background: rgba(96,165,250,0.15) !important; color: #60a5fa !important; border-bottom: 2px solid #60a5fa !important; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: rgba(15,23,42,0.4); }
::-webkit-scrollbar-thumb { background: rgba(96,165,250,0.4); border-radius: 5px; }
::-webkit-scrollbar-thumb:hover { background: rgba(96,165,250,0.6); }
</style>""", unsafe_allow_html=True)

st.markdown("<div class='main-header'>HoaxRaja - Analisa Hoax Indonesia</div>", unsafe_allow_html=True)
st.markdown("<div class='sub-header'>Analisis pola bahasa disinformasi dan verifikasi fakta dengan Google Gemini</div>", unsafe_allow_html=True)


# --- Sidebar ---
with st.sidebar:
    st.header("Pengaturan")
    # Prioritas: Streamlit Secrets > .env > input manual user
    try:
        API_KEY = st.secrets.get("GEMINI_API_KEY", None)
    except Exception:
        API_KEY = None
    if not API_KEY:
        API_KEY = os.getenv("GEMINI_API_KEY")
    if not API_KEY:
        API_KEY = st.text_input(
            "GEMINI_API_KEY", type="password", placeholder="AIza...",
            help="Ambil gratis di: https://aistudio.google.com/app/apikey"
        )
    if not API_KEY:
        st.warning("Masukkan GEMINI_API_KEY di Secrets (production) atau sidebar (dev)")
    st.divider()
    # Fixed model: Gemini 3.5 Flash
    model_name = "gemini-3.5-flash"
    st.subheader("Opsi Analisis")
    use_search = st.checkbox(
        "Verifikasi via Google Search (Grounding)", value=False,
        help="Aktifkan untuk pencarian fakta otomatis"
    )
    st.divider()
    st.subheader("Cek Fakta")
    st.markdown("- [TurnBackHoax.id](https://turnbackhoax.id/)")
    st.markdown("- [Mafindo.org](https://mafindo.org/)")
    st.markdown("- [CekFakta.com](https://cekfakta.com/)")
    st.markdown("- [Reuters Fact Check](https://www.reuters.com/fact-check/)")
    st.divider()
    st.subheader("Sumber Berita")
    st.caption("Pilih media Indonesia untuk pencarian artikel terkait.")
    # Multiselect dengan default semua aktif
    default_sources = list(_NEWS_SOURCES.keys())
    selected_sources = st.multiselect(
        "Aktifkan sumber:",
        options=list(_NEWS_SOURCES.keys()),
        default=default_sources,
        format_func=lambda x: _NEWS_SOURCES[x]["display_name"],
        label_visibility="collapsed",
    )
    if not selected_sources:
        st.warning("Minimal 1 sumber harus dipilih.")
        selected_sources = default_sources


# --- Main Tabs ---
tab1, tab2, tab3 = st.tabs(["Analisis", "Riwayat", "Edukasi"])



# ============================================================
# TAB 1: ANALYSIS
# ============================================================
with tab1:
    st.subheader("Tempelkan teks berita/artikel/chat WA")
    teks = st.text_area(
        "Teks yang akan dianalisis",
        height=180,
        placeholder="Contoh klaim lengkap: '[Nama Tokoh] dikabarkan meninggal dunia dalam kecelakaan'\n(WAJIB lengkap dengan klaimnya, bukan cuma nama orang. Minimal 4 kata.)",
        label_visibility="collapsed",
        help="Masukkan klaim lengkap minimal 4 kata. Nama saja tanpa konteks tidak bisa dianalisis.",
        key="input_teks",
    )
    # Show example helper
    with st.expander("Lihat contoh teks yang baik"):
        st.markdown("""
        **Contoh teks yang baik (akan dianalisis dengan akurat):**
        - "[Nama Tokoh] dikabarkan meninggal dunia dalam kecelakaan di Jakarta"
        - "Vaksin COVID-19 mengandung chip 5G yang bisa mengendalikan pikiran"
        - "Gibran resmi menjadi presiden setelah pengunduran diri Jokowi"
        - "Bantuan PKH bulan ini dicairkan Rp 10 juta per keluarga"

        **Contoh teks yang BURUK (tidak akan dianalisis):**
        - "[Nama Tokoh]" (cuma nama)
        - "Info penting!!!" (tanpa konteks)
        - "Viral" (tanpa klaim)
        """)
    col_btn1, col_btn2 = st.columns([3, 1])
    with col_btn1:
        mulai = st.button("Mulai Analisis", type="primary", use_container_width=True)
    with col_btn2:
        clear_btn = st.button("Clear", use_container_width=True)

    if clear_btn:
        # Clear text_area dengan reset widget value via session_state
        st.session_state.input_teks = ""
        st.session_state.last_result = None
        st.rerun()

    # Inisialisasi loading state
    if "is_analyzing" not in st.session_state:
        st.session_state.is_analyzing = False
    if "last_result" not in st.session_state:
        st.session_state.last_result = None

    if mulai:
        valid, msg = validate_teks_input(teks)
        if not valid:
            st.error(msg)
        elif not API_KEY:
            st.error("Masukkan GEMINI_API_KEY di sidebar untuk memulai analisis.")
        else:
            # Fetch scraped articles SEBELUM analisis_hoax
            with st.spinner("Mencari artikel terkait di media Indonesia..."):
                news_results = search_news_multi_source(
                    teks,
                    max_per_source=3,
                    enabled_sources=selected_sources,
                    fetch_body=True,
                )
            with st.spinner("Gemini sedang menganalisis... Mohon tunggu..."):
                try:
                    hasil, sumber = analisis_hoax(
                        teks, API_KEY,
                        scraped_articles=news_results
                    )
                    st.success("Analisis selesai!")
                    st.divider()

                    c1, c2 = st.columns([2, 1])
                    with c1:
                        st.subheader("Probabilitas Hoax")
                        st.progress(hasil.probabilitas_hoax / 100)
                    with c2:
                        st.metric(
                            label="Skor Hoax",
                            value=str(hasil.probabilitas_hoax) + "%",
                            delta=hasil.kesimpulan
                        )

                    stype, slabel = get_status(hasil.probabilitas_hoax)
                    # Simpan ke history (persistent)
                    try:
                        add_history_entry(teks, hasil, slabel)
                    except Exception as hist_err:
                        logger.warning("Gagal simpan history: %s", hist_err)
                    # Cache hasil untuk re-render
                    st.session_state.last_result = {
                        "hasil": hasil,
                        "sumber": sumber,
                        "slabel": slabel,
                        "stype": stype,
                        "teks": teks,
                    }
                    if stype == "success":
                        st.success("**Status: " + slabel + "** - Teks tampak netral/informatif.")
                    elif stype == "warning":
                        st.warning("**Status: " + slabel + "** - Disarankan verifikasi lebih lanjut ke sumber resmi.")
                    else:
                        st.error("**Status: " + slabel + "** - Jangan disebarkan sebelum diverifikasi ke sumber resmi.")
                    st.divider()

                    # --- TurnBackHoax Widget ---
                    st.subheader("Cek di TurnBackHoax.id")
                    tbh_results = []
                    tbh_error = None
                    with st.spinner("Mencari artikel terkait di TurnBackHoax..."):
                        try:
                            # Use the validated search function directly
                            tbh_results = search_turnbackhoax(teks, max_results=5)
                        except Exception as tbh_e:
                            tbh_error = str(tbh_e)
                            logger.error("TBH widget error: %s", tbh_e)

                    if tbh_error:
                        st.warning(
                            "Gagal menghubungi TurnBackHoax.id (" + tbh_error + "). "
                            "Anda masih bisa verifikasi manual di [turnbackhoax.id](https://turnbackhoax.id/)."
                        )
                    elif tbh_results:
                        st.success("Ditemukan " + str(len(tbh_results)) + " artikel relevan di TurnBackHoax.id")
                        for item in tbh_results:
                            hoax_class = "tbh-badge-default"
                            if item["hoax_type"] == "SALAH": hoax_class = "tbh-badge-salah"
                            elif item["hoax_type"] == "PENIPUAN": hoax_class = "tbh-badge-penipuan"
                            elif item["hoax_type"] == "KLARIFIKASI": hoax_class = "tbh-badge-fakta"
                            elif item["hoax_type"] == "LEBIKAN": hoax_class = "tbh-badge-lebikan"
                            badge = "<span class='tbh-badge " + hoax_class + "'>" + (item["hoax_type"] or "INFO") + "</span>"
                            relevance_badge = "<span class='tbh-badge tbh-badge-fakta' style='margin-left:6px;'>RELEVAN " + str(int(item.get("relevance", 0) * 100)) + "%</span>"
                            excerpt_html = "<br/><small>" + item["excerpt"][:150] + "...</small>" if item["excerpt"] else ""
                            date_html = "<small style='color:#94a3b8;'>" + item["date"] + "</small>" if item["date"] else ""
                            html = "<div class='tbh-widget'>" + badge + relevance_badge + "<br/><strong><a href='" + item["url"] + "' target='_blank'>" + item["title"] + "</a></strong>" + date_html + excerpt_html + "</div>"
                            st.markdown(html, unsafe_allow_html=True)
                    else:
                        st.warning("Tidak ada artikel spesifik di TurnBackHoax.id untuk klaim ini. Kemungkinan klaim ini BELUM diverifikasi oleh TurnBackHoax, atau topiknya tidak terkait dengan isu Indonesia. Jangan langsung percaya, verifikasi manual ke sumber resmi.")
                    st.divider()

                    # --- Multi-Source News Widget ---
                    st.subheader("📰 Pencarian di Media Indonesia")
                    with st.spinner("Mencari artikel di " + str(len(selected_sources)) + " media..."):
                        news_results = search_news_multi_source(
                            teks,
                            max_per_source=3,
                            enabled_sources=selected_sources,
                            fetch_body=True,
                        )
                        # Kelompokkan per source untuk ringkasan
                        per_source_count = {}
                        for r in news_results:
                            sk = r.get("source_key", "?")
                            per_source_count[sk] = per_source_count.get(sk, 0) + 1

                    if news_results:
                        sources_found = sorted(per_source_count.keys())
                        st.success(
                            "Ditemukan " + str(len(news_results)) + " artikel dari "
                            + str(len(sources_found)) + " media: "
                            + ", ".join(sources_found)
                        )
                        for item in news_results:
                            tag = item.get("source_tag", "?")
                            cek_fakta_html = (
                                '<span class="cek-fakta-badge">🛡️ CEK FAKTA</span>'
                                if item.get("is_cek_fakta") else ""
                            )
                            source_badge = (
                                '<span class="source-badge source-' + tag + '">'
                                + tag + '</span>'
                            )
                            html = (
                                '<div class="news-widget">'
                                + source_badge + cek_fakta_html
                                + '<br/><strong><a href="' + item["url"]
                                + '" target="_blank">' + item["title"]
                                + '</a></strong></div>'
                            )
                            st.markdown(html, unsafe_allow_html=True)
                    else:
                        st.info(
                            "Tidak ada artikel relevan ditemukan di media Indonesia untuk klaim ini. "
                            "Bisa jadi klaim ini adalah hoax murni, atau topiknya belum diliput media. "
                            "Tetap waspada dan verifikasi manual."
                        )
                    st.divider()



                    # --- Results ---
                    if use_search and hasil.ringkasan_verifikasi:
                        st.subheader("Hasil Verifikasi Fakta (Google Search)")
                        st.info(hasil.ringkasan_verifikasi)

                    st.subheader("Analisis Bahasa")
                    st.info(hasil.analisis_bahasa)

                    st.subheader("Saran Cek Fakta")
                    st.info(hasil.cek_fakta)

                    st.subheader("Kesimpulan")
                    st.markdown("**" + hasil.kesimpulan + "**")

                    if use_search and sumber:
                        st.divider()
                        st.subheader("Sumber dari Web (" + str(len(sumber)) + ")")
                        st.caption("URL yang digunakan Gemini untuk memverifikasi klaim:")
                        for i, s in enumerate(sumber, 1):
                            st.markdown(str(i) + ". [" + s["title"] + "](" + s["uri"] + ")")

                    # --- Share ---
                    st.divider()
                    st.subheader("Bagikan Hasil")
                    share_text = generate_share_text(teks, hasil.probabilitas_hoax, hasil.kesimpulan)
                    st.text_area(
                        "Salin teks hasil analisis:",
                        value=share_text,
                        height=150,
                        label_visibility="collapsed",
                        key="share_ta"
                    )
                    col_copy, col_tw, col_wa = st.columns(3)
                    with col_copy:
                        st.button("Copy ke Clipboard", key="copy_btn")
                    with col_tw:
                        tw_url = "https://twitter.com/intent/tweet?text=" + urllib.parse.quote(share_text[:200])
                        st.markdown("[Tweet](" + tw_url + ")", unsafe_allow_html=True)
                    with col_wa:
                        wa_url = "https://wa.me/?text=" + urllib.parse.quote(share_text)
                        st.markdown("[WhatsApp](" + wa_url + ")", unsafe_allow_html=True)

                    with st.expander("Raw JSON (debug)"):
                        st.code(
                            json.dumps(
                                json.loads(hasil.model_dump_json()),
                                indent=2, ensure_ascii=False
                            ),
                            language="json"
                        )

                except (HoaxAnalysisError, ValueError, KeyError, AttributeError) as e:
                    logger.exception("Analisis hoax gagal")
                    err = str(e)
                    err_lower = err.lower()
                    if any(x in err for x in ["API key", "401", "403", "unauthenticated"]):
                        st.error("API Key tidak valid. Periksa GEMINI_API_KEY di file .env.")
                    elif any(x in err_lower for x in ["429", "quota", "rate limit"]):
                        st.error("Rate limit terlampaui. Tunggu beberapa saat lalu coba lagi.")
                    elif any(x in err for x in ["MAX_TOKENS", "melebihi", "max output"]):
                        st.error("Respons terpotong (melebihi token limit). Coba perpendek input.")
                    elif any(x in err_lower for x in ["timeout", "deadline"]):
                        st.error("Request timeout. Coba lagi atau periksa koneksi internet.")
                    elif isinstance(e, ValidationError):
                        st.error("Format respons Gemini tidak sesuai schema. Coba lagi.")
                        with st.expander("Detail error"):
                            st.code(str(e))
                    elif isinstance(e, InvalidResponseError):
                        st.error("Respons Gemini tidak valid. Coba lagi atau ubah teks input.")
                    else:
                        st.error("Terjadi error: " + err)




# ============================================================
# TAB 2: HISTORY
# ============================================================
with tab2:
    st.subheader("Riwayat Analisis")
    if "history" not in st.session_state:
        init_history()

    history_list = st.session_state.history or []
    total = len(history_list)
    display_list = history_list[-HISTORY_DISPLAY_LIMIT:]  # 10 terakhir

    # Tombol refresh + hapus
    col_refresh, col_clear = st.columns([3, 1])
    with col_refresh:
        if st.button("Refresh dari Disk", help="Reload history dari file JSON", use_container_width=True):
            st.session_state.history = _load_history_from_disk()
            st.rerun()
    with col_clear:
        confirm_clear = st.button("Hapus Semua", type="secondary", use_container_width=True)

    if confirm_clear:
        clear_history()
        st.success("Riwayat dihapus.")
        st.rerun()

    if history_list:
        st.caption(f"Menampilkan {len(display_list)} dari total {total} analisis. Disimpan di: `{HISTORY_FILE}`")
        for item in reversed(display_list):
            with st.expander(
                "[" + item["waktu"] + "] Skor: " + str(item["skor"]) + "% - " + item["status"],
                expanded=False,
            ):
                st.text("Teks : " + item["teks"][:150] + "...")
                st.metric("Skor", str(item["skor"]) + "%")
                st.markdown("**Status:** " + item["status"])
                st.markdown("**Analisis:** " + item["analisis"][:200] + "...")
    else:
        st.info("Belum ada riwayat analisis. Lakukan analisis pertama Anda!")


# ============================================================
# TAB 3: EDUCATION
# ============================================================
with tab3:
    st.subheader("Edukasi: Cara Mengenali Hoax")
    st.markdown("""### Tips Mengenali Berita Palsu (Hoax)
1. **Cek Sumber** - Apakah sumbernya terpercaya? (Kompas, Detik, BBC, dll)
2. **Perhatikan Clickbait** - Judul berlebihan seperti "BREAKING!", "WAJIB BACA!"
3. **Cek Tanggal** - Berita lama di-share ulang tanpa konteks
4. **Klaim Tanpa Sumber** - Tidak ada nama pejabat/lembaga resmi
5. **Emoi Berlebihan** - Caps Lock, tanda seru berlebihan""")

    col_ef1, col_ef2 = st.columns(2)
    with col_ef1:
        st.markdown("**Situs Cek Fakta:**")
        st.markdown("- [TurnBackHoax.id](https://turnbackhoax.id/)")
        st.markdown("- [Mafindo.org](https://mafindo.org/)")
        st.markdown("- [CekFakta.com](https://cekfakta.com/)")
    with col_ef2:
        st.markdown("**International:**")
        st.markdown("- [Reuters Fact Check](https://www.reuters.com/fact-check/)")
        st.markdown("- [Snopes.com](https://www.snopes.com/)")
        st.markdown("- [BBC Reality Check](https://www.bbc.com/news/reality_check)")

    st.divider()
    st.markdown("""### Ciri Teks HOAX vs Teks AMAN
**Ciri HOAX:**
- Caps Lock berlebihan
- Klaim "sumber terpercaya" tanpa nama jelas
- Tidak ada tanggal kejadian
- Judul provokatif tidak sesuai isi
**Ciri AMAN:**
- Sumber resmi jelas (nama media, tanggal, nama jurnalis)
- Judul sesuai dengan isi
- Ada link ke sumber asli""")

# --- Footer ---
st.divider()
st.markdown(
    "<div class='author-credit'>Dikembangkan oleh <strong>Raja Adedia Davarel Pratama</strong><br/><em>Dibuat dengan bantuan AI</em></div>",
    unsafe_allow_html=True,
)


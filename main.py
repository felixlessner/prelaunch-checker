
import re, uuid, threading
from urllib.parse import urljoin, urlparse
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests as rq
from bs4 import BeautifulSoup
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
from typing import Optional
from db import init_db, save_check_to_db

VERSION = "1.0.0"

app = FastAPI(title="Pre-Launch Checker")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(os.path.join("static", "favicon.ico"))

app.mount("/static", StaticFiles(directory="static"), name="static")

jobs: dict = {}
lock = threading.Lock()

ADOBE_DOMAIN_PAT = re.compile(
    r"(stock\.adobe\.com|fotolia\.com|as\d\.ftcdn\.net)",
    re.IGNORECASE,
)
ADOBE_PREVIEW_PAT = re.compile(
    r"[^/\?#]*preview[^/\?#]*\.(jpg|jpeg|png|webp|gif|svg)",
    re.IGNORECASE,
)

def make_session():
    s = rq.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (compatible; PreLaunchChecker/1.0)"
    return s

def normalize_start_url(u: str) -> str:
    u = u.strip()
    parsed = urlparse(u)

    # Wenn User explizit http/https angibt: so lassen
    if parsed.scheme in ("http", "https"):
        return u

    if not parsed.scheme:
        # Erst https probieren, bei Fehler auf http zurückfallen
        https_url = "https://" + u
        try:
            r = rq.head(https_url, timeout=4, allow_redirects=True)
            if r.status_code < 400:
                return https_url
        except Exception:
            pass
        return "http://" + u

    # Andere Schemes (ftp, mailto, …) kannst du ggf. ablehnen
    raise ValueError("Nur http und https werden unterstützt.")

def safe_get(session, url, timeout=12):
    try:
        return session.get(url, timeout=timeout, allow_redirects=True), None
    except rq.exceptions.TooManyRedirects:
        return None, "Too many redirects"
    except rq.exceptions.ConnectionError as e:
        return None, f"Connection error: {e}"
    except rq.exceptions.Timeout:
        return None, "Timeout"
    except Exception as e:
        return None, str(e)

def safe_head(session, url, timeout=8):
    try:
        return session.head(url, timeout=timeout, allow_redirects=True), None
    except Exception as e:
        return None, str(e)

def normalize(base, href):
    try:
        url = urljoin(base, href.strip())
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return None
        return p._replace(fragment="").geturl()
    except Exception:
        return None

def same_domain(base, url):
    return urlparse(url).netloc == urlparse(base).netloc

def is_html_url(url):
    """Gibt False zurück wenn die URL auf eine Nicht-HTML-Ressource zeigt (Bild, Dokument, etc.)."""
    NON_HTML_EXT = {
        'jpg','jpeg','png','gif','webp','svg','ico','bmp','tiff','avif',
        'pdf','doc','docx','xls','xlsx','ppt','pptx','odt','ods','odp','csv',
        'zip','rar','gz','tar','7z',
        'mp3','mp4','wav','ogg','avi','mov','wmv','flv','webm',
        'woff','woff2','ttf','eot','otf',
        'js','css','xml','json','rss','atom',
    }
    path = urlparse(url).path.lower()
    ext = path.rsplit('.', 1)[-1] if '.' in path.split('/')[-1] else ''
    return ext not in NON_HTML_EXT

def filename_from_url(url):
    path = urlparse(url).path
    return path.split("/")[-1] if "/" in path else path

def check_adobe_preview(html, url):
    hits = []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["img", "source"]):
        for attr in ["src", "data-src", "srcset"]:
            val = tag.get(attr, "")
            if not val:
                continue
            candidates = [v.strip().split(" ")[0] for v in val.split(",")]
            for candidate in candidates:
                if not candidate:
                    continue
                abs_url = normalize(url, candidate) or candidate
                fname = filename_from_url(abs_url)
                is_adobe_domain = bool(ADOBE_DOMAIN_PAT.search(abs_url))
                is_preview_name = bool(ADOBE_PREVIEW_PAT.search(fname))
                if is_adobe_domain or is_preview_name:
                    reason = []
                    if is_adobe_domain:
                        reason.append("Adobe/Fotolia-Domain")
                    if is_preview_name:
                        reason.append('Dateiname enthält "preview"')
                    hits.append({"img_url": abs_url[:300], "reason": ", ".join(reason), "tag": tag.name})
    seen = set()
    unique = []
    for h in hits:
        if h["img_url"] not in seen:
            seen.add(h["img_url"])
            unique.append(h)
    return unique[:30]

def check_favicon(soup, origin, session):
    link_tag = soup.find("link", rel=lambda r: r and any(
        x in [v.lower() for v in (r if isinstance(r, list) else [r])]
        for x in ["icon", "shortcut icon"]
    ))
    if not link_tag:
        ico_url = f"{origin}/favicon.ico"
        resp, err = safe_head(session, ico_url)
        ico_ok = resp is not None and resp.status_code == 200
        return {
            "tag_present": False, "found": ico_ok,
            "href": ico_url if ico_ok else None, "correct_name": False,
            "status_code": resp.status_code if resp else None,
            "note": "Kein <link rel=\"icon\"> im HTML. favicon.ico im Root " + ("gefunden." if ico_ok else "ebenfalls nicht gefunden."),
            "status": "error" if not ico_ok else "warning",
        }
    href = link_tag.get("href", "")
    abs_href = normalize(origin, href) or href
    fname = filename_from_url(abs_href)
    correct_name = fname.lower() == "favicon.png"
    resp, err = safe_head(session, abs_href)
    reachable = resp is not None and resp.status_code == 200
    note_parts = []
    if not correct_name:
        note_parts.append(f'Dateiname ist "{fname}" – empfohlen: "favicon.png"')
    if not reachable:
        note_parts.append(f"Datei nicht erreichbar (HTTP {resp.status_code if resp else err})")
    status = "ok" if reachable else "error"
    if reachable and not correct_name:
        status = "warning"
    return {
        "tag_present": True, "found": reachable, "href": abs_href,
        "correct_name": correct_name,
        "status_code": resp.status_code if resp else None,
        "note": "; ".join(note_parts) if note_parts else "Favicon korrekt eingebunden.",
        "status": status,
    }

def check_apple_touch_icon(soup, origin, session):
    link_tag = soup.find("link", rel=lambda r: r and "apple-touch-icon" in (
        " ".join(r) if isinstance(r, list) else r
    ).lower())
    if not link_tag:
        return {
            "tag_present": False, "found": False, "href": None, "correct_name": False,
            "status_code": None,
            "note": "Kein <link rel=\"apple-touch-icon\"> im HTML gefunden.",
            "status": "error",
        }
    href = link_tag.get("href", "")
    abs_href = normalize(origin, href) or href
    fname = filename_from_url(abs_href)
    correct_name = fname.lower() == "apple-touch-icon.png"
    resp, err = safe_head(session, abs_href)
    reachable = resp is not None and resp.status_code == 200
    note_parts = []
    if not correct_name:
        note_parts.append(f'Dateiname ist "{fname}" – empfohlen: "apple-touch-icon.png"')
    if not reachable:
        note_parts.append(f"Datei nicht erreichbar (HTTP {resp.status_code if resp else err})")
    status = "ok" if reachable else "error"
    if reachable and not correct_name:
        status = "warning"
    return {
        "tag_present": True, "found": reachable, "href": abs_href,
        "correct_name": correct_name,
        "status_code": resp.status_code if resp else None,
        "note": "; ".join(note_parts) if note_parts else "Apple Touch Icon korrekt eingebunden.",
        "status": status,
    }

def crawl(start_url: str, max_pages: int, job_id: str):
    session = make_session()
    visited = set()
    queue = [start_url]
    pages = []

    with lock:
        jobs[job_id]["status"] = "crawling"
        jobs[job_id]["progress"] = 5

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        resp, err = safe_get(session, url)
        page = {"url": url, "error": err, "status_code": None, "html": None, "final_url": url}
        if resp is not None:
            page["status_code"] = resp.status_code
            page["final_url"] = str(resp.url)
            ct = resp.headers.get("content-type", "")
            if "text/html" in ct:
                page["html"] = resp.text
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = normalize(url, a["href"])
                    if href and same_domain(start_url, href) and href not in visited and is_html_url(href):
                        queue.append(href)
        pages.append(page)
        with lock:
            jobs[job_id]["progress"] = min(50, 5 + int(len(pages)/max_pages*45))

    with lock:
        jobs[job_id]["status"] = "checking"
        jobs[job_id]["progress"] = 55

    base_parsed = urlparse(start_url)
    origin = f"{base_parsed.scheme}://{base_parsed.netloc}"

    robots_url = f"{origin}/robots.txt"
    robots_resp, _ = safe_get(session, robots_url)
    robots_content = ""
    robots_ok = False
    if robots_resp and robots_resp.status_code == 200:
        robots_content = robots_resp.text
        robots_ok = True

    site_summary = {
        "robots_txt": {
            "found": robots_ok,
            "url": robots_url,
            "content_preview": robots_content[:600] if robots_ok else "",
            "disallows_all": "Disallow: /" in robots_content,
        },
    }

    results = []
    # all_links: dict { href -> list of {page_url, anchor_text} }
    all_links: dict = {}

    for i, p in enumerate(pages):
        if p.get("error") or not p.get("html"):
            results.append({"url": p["url"], "error": p.get("error","No HTML"), "status_code": p.get("status_code"), "checks": {}})
            continue

        soup = BeautifulSoup(p["html"], "html.parser")
        checks = {}

        # Indexierbarkeit
        meta_robots = soup.find("meta", attrs={"name": re.compile(r"robots", re.I)})
        robots_tag_content = ""
        noindex = False
        if meta_robots:
            robots_tag_content = meta_robots.get("content", "")
            noindex = "noindex" in robots_tag_content.lower()
        canonical = soup.find("link", rel="canonical")
        canonical_href = canonical["href"] if canonical else None
        path = urlparse(p["url"]).path
        disallowed = False
        if robots_ok:
            for line in robots_content.splitlines():
                line = line.strip()
                if line.lower().startswith("disallow:"):
                    dp = line[9:].strip()
                    if dp and path.startswith(dp):
                        disallowed = True
                        break
        checks["indexability"] = {
            "indexable": not noindex,
            "meta_robots": robots_tag_content or "nicht gesetzt",
            "noindex": noindex,
            "canonical": canonical_href,
            "robots_txt_disallows": disallowed,
        }

        # Title
        title_tag = soup.find("title")
        title_text = title_tag.get_text(strip=True) if title_tag else ""
        title_len = len(title_text)
        checks["title"] = {
            "text": title_text, "length": title_len,
            "missing": not title_text,
            "too_short": title_len < 30 and bool(title_text),
            "too_long": title_len > 60,
        }

        # Meta Description
        meta_desc = soup.find("meta", attrs={"name": re.compile(r"description", re.I)})
        desc_text = meta_desc["content"].strip() if meta_desc and meta_desc.get("content") else ""
        desc_len = len(desc_text)
        checks["meta_description"] = {
            "text": desc_text, "length": desc_len,
            "missing": not desc_text,
            "too_short": desc_len < 70 and bool(desc_text),
            "too_long": desc_len > 160,
        }

        # H1
        h1_tags = soup.find_all("h1")
        h1_texts = [h.get_text(strip=True) for h in h1_tags]
        checks["h1"] = {
            "count": len(h1_tags), "texts": h1_texts,
            "missing": len(h1_tags) == 0, "multiple": len(h1_tags) > 1,
        }

        # Links sammeln – mit Ankertext und Quellseite
        page_links = []
        for a in soup.find_all("a", href=True):
            href = normalize(p["url"], a["href"])
            if not href:
                continue
            anchor_text = a.get_text(strip=True)[:120] or "(kein Ankertext)"
            page_links.append(href)
            if href not in all_links:
                all_links[href] = []
            # Maximal 5 Quellen pro Link speichern
            if len(all_links[href]) < 5:
                all_links[href].append({
                    "page_url": p["url"],
                    "anchor_text": anchor_text,
                })
        checks["links"] = {"count": len(page_links), "links": page_links[:100]}

        # Adobe Stock
        adobe_hits = check_adobe_preview(p["html"], p["url"])
        checks["adobe_stock"] = {
            "found": len(adobe_hits) > 0,
            "count": len(adobe_hits),
            "samples": adobe_hits,
        }

        # Favicon + Apple Touch Icon (nur erste Seite)
        if i == 0:
            checks["favicon"] = check_favicon(soup, origin, session)
            checks["apple_touch_icon"] = check_apple_touch_icon(soup, origin, session)
        else:
            checks["favicon"] = None
            checks["apple_touch_icon"] = None

        results.append({
            "url": p["url"],
            "status_code": p.get("status_code"),
            "final_url": p.get("final_url"),
            "checks": checks,
        })

    with lock:
        jobs[job_id]["progress"] = 80

    # Broken Link Check – nur interne Links, mit Quellseite + Ankertext
    broken_links = []
    internal_links = {lnk: src for lnk, src in all_links.items() if same_domain(start_url, lnk)}
    for lnk, sources in list(internal_links.items())[:80]:
        try:
            r = session.head(lnk, timeout=8, allow_redirects=True)
            if r.status_code >= 400:
                broken_links.append({
                    "url": lnk,
                    "status": r.status_code,
                    "sources": sources,
                })
        except Exception as e:
            broken_links.append({
                "url": lnk,
                "status": "error",
                "detail": str(e),
                "sources": sources,
            })

    with lock:
        jobs[job_id]["status"] = "done"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["result"] = {
            "site_summary": site_summary,
            "pages": results,
            "broken_links": broken_links,
            "crawled_count": len(results),
        }
        # Kopie der Job-Daten für DB-Speicherung
        job_data = jobs[job_id].copy()

    # Außerhalb des Locks in die DB schreiben
    try:
        save_check_to_db(
            check_id=job_id,
            start_url=job_data.get("start_url"),
            customer_id=job_data.get("customer_id"),
            result=job_data["result"],
            status=job_data.get("status", "done"),
        )
    except Exception:
        # Hier könntest du noch Logging ergänzen
        # z.B. logging.exception("Konnte Check nicht in DB speichern")
        pass

class StartRequest(BaseModel):
    url: str
    max_pages: int = 20
    customer_id: Optional[str] = None

@app.post("/api/start")
def start_check(req: StartRequest, bg: BackgroundTasks):
    # NEU: URL normalisieren
    start_url = normalize_start_url(req.url)

    job_id = str(uuid.uuid4())
    with lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0,
            "result": None,
            "customer_id": req.customer_id,
            "start_url": start_url,
        }
    # NEU: normalisierte URL an crawl übergeben
    bg.add_task(crawl, start_url, req.max_pages, job_id)
    return {"job_id": job_id}

@app.get("/api/status/{job_id}")
def get_status(job_id: str):
    with lock:
        job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return {"status": job["status"], "progress": job["progress"], "result": job["result"]}

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": VERSION,
        }

@app.get("/", response_class=HTMLResponse)
def index():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()

# Ganz unten in main.py hinzufügen:

from api import register_routes

register_routes(
    app=app,
    make_session=make_session,
    safe_get=safe_get,
    safe_head=safe_head,
    normalize=normalize,
    same_domain=same_domain,
    is_html_url=is_html_url,
    filename_from_url=filename_from_url,
    check_adobe_preview=check_adobe_preview,
    check_favicon=check_favicon,
    check_apple_touch_icon=check_apple_touch_icon,
)

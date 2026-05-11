"""
Synchronous API for Pre-Launch Checker.

Stellt zwei Endpoints bereit:

    GET  /api/v1/check
    POST /api/v1/check

Input (JSON-Body):
    {
        "URL": "https://example.com",
        "max_pages": 20
    }

Output:
    {
        "site_summary": { ... },
        "pages": [ ... ],
        "broken_links": [ ... ],
        "crawled_count": 10
    }

Authentifizierung:
    - Header: "x-api-key: <DEIN_API_KEY>"
    - Erwarteter Wert kommt aus der Umgebungsvariable API_KEY.
      Ist API_KEY nicht gesetzt, ist die API ungeschützt (z.B. für lokale Entwicklung).

Die eigentliche Logik (HTTP-Session, HTML-Parsing, etc.) wird aus main.py
per Dependency Injection hereingereicht (siehe register_routes in main.py),
dadurch vermeiden wir zirkuläre Importe.
"""

import os
import re
from typing import Any, Dict, List, Set, Callable, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from fastapi import Body, HTTPException, Depends, Header
from pydantic import BaseModel, HttpUrl, Field


class CheckRequest(BaseModel):
    """
    Request-Body für den synchronen Check-Endpoint.

    Beachte:
    - Feldname im JSON ist "URL".
    - In Python arbeiten wir mit dem Attribut "url".
    """

    url: HttpUrl = Field(alias="URL")
    max_pages: int = 20

    class Config:
        populate_by_name = True  # erlaubt auch "url" im JSON, falls gewünscht


def register_routes(
    app,
    make_session: Callable[[], Any],
    safe_get: Callable[..., Any],
    safe_head: Callable[..., Any],
    normalize: Callable[[str, str], str],
    same_domain: Callable[[str, str], bool],
    is_html_url: Callable[[str], bool],
    filename_from_url: Callable[[str], str],
    check_adobe_preview: Callable[[str, str], Any],
    check_favicon: Callable[[Any, str, Any], Dict[str, Any]],
    check_apple_touch_icon: Callable[[Any, str, Any], Dict[str, Any]],
) -> None:
    """
    Wird von main.py aufgerufen und hängt die Routen an das bestehende FastAPI-App-Objekt.

    Wichtig: Keine Importe von main.py hier drin – alle Abhängigkeiten werden
    von außen übergeben. So gibt es keinen zirkulären Import.
    """

    # ------------------------------------------------------------------
    # API-Key-Authentifizierung
    # ------------------------------------------------------------------

    API_KEY_ENV_NAME = "API_KEY"

    def verify_api_key(
        x_api_key: Optional[str] = Header(
            default=None,
            description="API key for authentication",
        )
    ) -> None:
        """
        Prüft den API-Key aus dem Header `x-api-key`.

        - Erwarteter Key kommt aus der Umgebungsvariable API_KEY.
        - Ist API_KEY nicht gesetzt oder leer, wird NICHT geprüft
          (praktisch für lokale Entwicklung).
        """
        expected = os.getenv(API_KEY_ENV_NAME)

        # Kein API-Key konfiguriert -> kein Schutz (Dev-Modus)
        if not expected:
            return

        if x_api_key != expected:
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing API key",
            )

    # ------------------------------------------------------------------
    # Crawl- und Check-Logik (unverändert)
    # ------------------------------------------------------------------

    def run_checks(start_url: str, max_pages: int = 20) -> Dict[str, Any]:
        """
        Führt die Crawl- und Check-Logik synchron aus.

        Nutzt dieselben Helfer wie die bestehende Logik in main.py
        (make_session, safe_get, usw.), aber ohne das jobs-Dict zu verwenden.
        """

        session = make_session()
        visited: Set[str] = set()
        queue: List[str] = [start_url]
        pages: List[Dict[str, Any]] = []

        # Crawl bis zu max_pages HTML-Seiten auf derselben Domain
        while queue and len(pages) < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            resp, err = safe_get(session, url)
            page: Dict[str, Any] = {
                "url": url,
                "error": err,
                "status_code": None,
                "html": None,
                "final_url": url,
            }

            if resp is not None:
                page["status_code"] = resp.status_code
                page["final_url"] = str(resp.url)
                ct = resp.headers.get("content-type", "")
                if "text/html" in ct:
                    page["html"] = resp.text
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for a in soup.find_all("a", href=True):
                        href = normalize(url, a["href"])
                        if (
                            href
                            and same_domain(start_url, href)
                            and href not in visited
                            and is_html_url(href)
                        ):
                            queue.append(href)

            pages.append(page)

        # robots.txt summary (analog zu main.py)
        base_parsed = urlparse(start_url)
        origin = f"{base_parsed.scheme}://{base_parsed.netloc}"

        robots_url = f"{origin}/robots.txt"
        robots_resp, _ = safe_get(session, robots_url)
        robots_content = ""
        robots_ok = False
        if robots_resp and robots_resp.status_code == 200:
            robots_content = robots_resp.text
            robots_ok = True

        site_summary: Dict[str, Any] = {
            "robots_txt": {
                "found": robots_ok,
                "url": robots_url,
                "content_preview": robots_content[:600] if robots_ok else "",
                "disallows_all": "Disallow: /" in robots_content,
            },
        }

        results: List[Dict[str, Any]] = []
        # all_links: dict { href -> list of {page_url, anchor_text} }
        all_links: Dict[str, List[Dict[str, str]]] = {}

        for i, p in enumerate(pages):
            if p.get("error") or not p.get("html"):
                # Seite konnte nicht geladen werden oder war kein HTML
                results.append(
                    {
                        "url": p["url"],
                        "error": p.get("error", "No HTML"),
                        "status_code": p.get("status_code"),
                        "final_url": p.get("final_url"),
                        "checks": {},
                    }
                )
                continue

            soup = BeautifulSoup(p["html"], "html.parser")
            checks: Dict[str, Any] = {}

            # Indexierbarkeit
            meta_robots = soup.find(
                "meta", attrs={"name": re.compile(r"robots", re.I)}
            )
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
                "text": title_text,
                "length": title_len,
                "missing": not title_text,
                "too_short": title_len < 30 and bool(title_text),
                "too_long": title_len > 60,
            }

            # Meta Description
            meta_desc = soup.find(
                "meta", attrs={"name": re.compile(r"description", re.I)}
            )
            desc_text = (
                meta_desc["content"].strip()
                if meta_desc and meta_desc.get("content")
                else ""
            )
            desc_len = len(desc_text)
            checks["meta_description"] = {
                "text": desc_text,
                "length": desc_len,
                "missing": not desc_text,
                "too_short": desc_len < 70 and bool(desc_text),
                "too_long": desc_len > 160,
            }

            # H1
            h1_tags = soup.find_all("h1")
            h1_texts = [h.get_text(strip=True) for h in h1_tags]
            checks["h1"] = {
                "count": len(h1_tags),
                "texts": h1_texts,
                "missing": len(h1_tags) == 0,
                "multiple": len(h1_tags) > 1,
            }

            # Links sammeln – mit Ankertext und Quellseite
            page_links: List[str] = []
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
                    all_links[href].append(
                        {
                            "page_url": p["url"],
                            "anchor_text": anchor_text,
                        }
                    )

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
                checks["apple_touch_icon"] = check_apple_touch_icon(
                    soup, origin, session
                )
            else:
                checks["favicon"] = None
                checks["apple_touch_icon"] = None

            results.append(
                {
                    "url": p["url"],
                    "status_code": p.get("status_code"),
                    "final_url": p.get("final_url"),
                    "checks": checks,
                }
            )

        # Broken Link Check – nur interne Links, mit Quellseite + Ankertext
        broken_links: List[Dict[str, Any]] = []
        internal_links = {
            lnk: src for lnk, src in all_links.items() if same_domain(start_url, lnk)
        }

        for lnk, sources in list(internal_links.items())[:80]:
            try:
                resp, err = safe_head(session, lnk, timeout=8)
                if resp is not None and resp.status_code >= 400:
                    broken_links.append(
                        {
                            "url": lnk,
                            "status": resp.status_code,
                            "sources": sources,
                        }
                    )
                elif resp is None:
                    broken_links.append(
                        {
                            "url": lnk,
                            "status": "error",
                            "detail": err,
                            "sources": sources,
                        }
                    )
            except Exception as e:  # Fallback
                broken_links.append(
                    {
                        "url": lnk,
                        "status": "error",
                        "detail": str(e),
                        "sources": sources,
                    }
                )

        return {
            "site_summary": site_summary,
            "pages": results,
            "broken_links": broken_links,
            "crawled_count": len(results),
        }

    # ------------------------------------------------------------------
    # Routen: GET + POST /api/v1/check (mit API-Key-Schutz)
    # ------------------------------------------------------------------

    @app.get("/api/v1/check")
    def api_v1_check_get(
        payload: CheckRequest = Body(...),
        _: None = Depends(verify_api_key),
    ) -> Dict[str, Any]:
        """
        Führt den Prelaunch-Check synchron aus und gibt das Ergebnis zurück.

        Request (JSON-Body):
            {
                "URL": "https://example.com",
                "max_pages": 20
            }

        Authentifizierung:
            Header: x-api-key: <DEIN_API_KEY>
        """
        try:
            result = run_checks(
                start_url=str(payload.url),
                max_pages=payload.max_pages,
            )
        except Exception as exc:
            # Safety-Net, damit der Client eine ordentliche Fehlermeldung bekommt
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return result

    @app.post("/api/v1/check")
    def api_v1_check_post(
        payload: CheckRequest = Body(...),
        _: None = Depends(verify_api_key),
    ) -> Dict[str, Any]:
        """
        POST-Variante des synchronen Prelaunch-Checks.

        Request (JSON-Body):
            {
                "URL": "https://example.com",
                "max_pages": 20
            }

        Authentifizierung:
            Header: x-api-key: <DEIN_API_KEY>
        """
        try:
            result = run_checks(
                start_url=str(payload.url),
                max_pages=payload.max_pages,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return result

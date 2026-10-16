#!/usr/bin/env python3
"""RidoMovies Nuvio addon — Closeload + RidoRapid. Port 51823."""
from __future__ import annotations

import base64
import re
import time
from typing import Any, Optional
from urllib.parse import quote, urljoin, urlparse

from flask import Flask, Response, jsonify, request

try:
    from curl_cffi import requests as cf_requests
except ImportError:
    import requests as cf_requests

app = Flask(__name__)
VERSION = "1.0.0"
ADDON_NAME = "Rido"
BASE_CANDIDATES = [
    "https://ridomovies.su",
    "https://ridomovies.tv",
    "https://ridomovie.to",
]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CACHE: dict = {}
CACHE_TTL = 600
ACTIVE_BASE: Optional[str] = None


def cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return resp


def cached(key: str, producer, ttl: int = CACHE_TTL):
    now = time.time()
    if key in CACHE and now - CACHE[key][0] < ttl:
        return CACHE[key][1]
    val = producer()
    CACHE[key] = (now, val)
    return val


def http_get(url: str, referer: str | None = None, as_json: bool = False) -> Any:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    kwargs = {"headers": headers, "timeout": 25}
    try:
        r = cf_requests.get(url, impersonate="chrome131", **kwargs)
    except TypeError:
        r = cf_requests.get(url, **kwargs)
    r.raise_for_status()
    return r.json() if as_json else r.text


def pick_base() -> str:
    global ACTIVE_BASE
    if ACTIVE_BASE:
        return ACTIVE_BASE
    for b in BASE_CANDIDATES:
        try:
            http_get(f"{b}/", referer=b)
            ACTIVE_BASE = b
            return b
        except Exception:
            continue
    ACTIVE_BASE = BASE_CANDIDATES[0]
    return ACTIVE_BASE


def unpack_packer(source: str) -> str:
    pattern = re.compile(r"}\('(.+?)',(\d+),(\d+),'(.+?)'\.split\('\|')", re.DOTALL)
    m = pattern.search(source)
    if not m:
        return source
    payload, radix, keywords = m.group(1), int(m.group(2)), m.group(4).split("|")
    chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def enc(n: int) -> str:
        return chars[n] if n < radix else enc(n // radix) + chars[n % radix]

    def repl(mo: re.Match) -> str:
        tok = mo.group(0)
        try:
            idx = int(tok, radix) if radix > 10 else int(tok)
        except ValueError:
            return tok
        if 0 <= idx < len(keywords) and keywords[idx]:
            return keywords[idx]
        return tok

    return re.sub(r"\b\w+\b", repl, payload)


def extract_packed_scripts(html: str) -> list:
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL | re.I)
    return [s for s in scripts if "p,a,c,k,e" in s or ".split('|')" in s]


def decode_closeload(embed_url: str, site_referer: str) -> Optional[str]:
    html = http_get(embed_url, referer=site_referer)
    for script in extract_packed_scripts(html):
        try:
            decoded = unpack_packer(script)
        except Exception:
            decoded = script
        m = re.search(r'var\s+\w+\s*=\s*"([A-Za-z0-9+/=]+)"', decoded)
        if not m:
            m = re.search(r'["\']([A-Za-z0-9+/]{40,}={0,2})["\']', decoded)
        if not m:
            continue
        try:
            url = base64.b64decode(m.group(1)).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if url.startswith("http"):
            return url
    m3 = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', html)
    return m3.group(1) if m3 else None


def decode_ridoo(embed_url: str, site_referer: str) -> Optional[str]:
    html = http_get(embed_url, referer=site_referer)
    m = re.search(r'file\s*:\s*"([^"]+)"', html) or re.search(r'file:"([^"]+)"', html)
    if m and m.group(1).startswith("http"):
        return m.group(1)
    m3 = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', html)
    return m3.group(1) if m3 else None


def api_search(q: str) -> list:
    base = pick_base()

    def _do():
        items = []
        for path in (f"{base}/core/api/search?q={quote(q)}", f"{base}/api/search?q={quote(q)}"):
            try:
                data = http_get(path, referer=base, as_json=True)
                raw = data.get("data") if isinstance(data, dict) else data
                if isinstance(raw, dict):
                    raw = raw.get("items") or raw.get("results") or []
                if not isinstance(raw, list):
                    continue
                for it in raw:
                    title = it.get("title") or it.get("name") or ""
                    year = ""
                    c = it.get("contentable") or {}
                    if isinstance(c, dict):
                        year = str(c.get("releaseYear") or c.get("year") or "")
                    year = year or str(it.get("year") or it.get("releaseYear") or "")
                    slug = (it.get("fullSlug") or it.get("slug") or "").lstrip("/")
                    if not title:
                        continue
                    items.append({
                        "id": str(it.get("id") or slug),
                        "title": title,
                        "year": year,
                        "fullSlug": slug,
                        "poster": it.get("poster") or it.get("image") or "",
                        "type": "series" if it.get("type") in ("tv", "series") or "series" in slug else "movie",
                    })
                if items:
                    break
            except Exception:
                continue
        return items

    return cached(f"search:{q.lower()}", _do, 300)


def fetch_iframes(full_slug: str, season=None, episode=None) -> list:
    base = pick_base()
    urls = []

    def from_videos(path: str):
        try:
            data = http_get(f"{base}/core/api{path}", referer=base, as_json=True)
            rows = data.get("data") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                return
            for row in rows:
                raw = row.get("url") or row.get("embed") or ""
                if not raw:
                    continue
                m = re.search(r'data-src=["\']([^"\']+)["\']', raw) or re.search(r'src=["\']([^"\']+)["\']', raw)
                if m:
                    urls.append(m.group(1))
                elif raw.startswith("http"):
                    urls.append(raw)
        except Exception:
            pass

    if season and episode:
        try:
            page = http_get(f"{base}/{full_slug}", referer=base)
            slug_ep = f"season-{season}/episode-{episode}"
            ids = re.findall(rf'\\"id\\":\\"(\d+)\\"(?=.*?\\"fullSlug\\":\\"[^"]*{re.escape(slug_ep)}[^"]*\\")', page)
            if not ids:
                ids = re.findall(rf'"id"\s*:\s*"?(\d+)"?[^}}]*{re.escape(slug_ep)}', page, re.I)
            if ids:
                from_videos(f"/episodes/{ids[-1]}/videos")
        except Exception:
            pass
    from_videos(f"/{full_slug}/videos")
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def resolve_streams(full_slug: str, season=None, episode=None) -> list:
    base = pick_base()
    streams = []
    for emb in fetch_iframes(full_slug, season, episode):
        if not emb.startswith("http"):
            emb = urljoin(base, emb)
        host = (urlparse(emb).hostname or "").lower()
        try:
            if "closeload" in host or "closeload" in emb:
                m3u8 = decode_closeload(emb, base + "/")
                if m3u8:
                    streams.append({
                        "name": "Rido",
                        "title": "Closeload\nFHD",
                        "url": m3u8,
                        "behaviorHints": {
                            "notWebReady": True,
                            "bingeGroup": f"rido-cl-{full_slug}",
                            "proxyHeaders": {"request": {
                                "Referer": "https://closeload.top/",
                                "Origin": "https://closeload.top",
                                "User-Agent": UA,
                            }},
                        },
                    })
            if "ridoo" in host or "rapid" in host or re.search(r"rido", host):
                m3u8 = decode_ridoo(emb, base + "/")
                if m3u8:
                    streams.append({
                        "name": "Rido",
                        "title": "RidoRapid\nFHD",
                        "url": m3u8,
                        "behaviorHints": {
                            "notWebReady": True,
                            "bingeGroup": f"rido-rp-{full_slug}",
                            "proxyHeaders": {"request": {
                                "Referer": base + "/",
                                "User-Agent": UA,
                            }},
                        },
                    })
        except Exception as e:
            app.logger.warning("embed fail %s: %s", emb, e)
    return streams


@app.route("/")
@app.route("/manifest.json")
def manifest():
    return cors(jsonify({
        "id": "community.rido.streams",
        "version": VERSION,
        "name": ADDON_NAME,
        "description": "RidoMovies — Closeload + RidoRapid",
        "resources": ["catalog", "meta", "stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["rido:"],
        "catalogs": [
            {"type": "movie", "id": "rido-search", "name": "Rido Search",
             "extra": [{"name": "search", "isRequired": True}]},
            {"type": "series", "id": "rido-search-series", "name": "Rido Series",
             "extra": [{"name": "search", "isRequired": True}]},
        ],
    }))


@app.route("/catalog/<type_>/<id_>.json")
@app.route("/catalog/<type_>/<id_>/search=<query>.json")
def catalog(type_, id_, query=None):
    q = query or request.args.get("search") or ""
    metas = []
    if q:
        for it in api_search(q)[:30]:
            metas.append({
                "id": f"rido:{it['fullSlug']}",
                "type": it.get("type") or type_,
                "name": it["title"],
                "poster": it.get("poster") or None,
                "releaseInfo": it.get("year") or "",
            })
    return cors(jsonify({"metas": metas}))


@app.route("/meta/<type_>/<path:id_>.json")
def meta(type_, id_):
    slug = id_[5:] if id_.startswith("rido:") else id_
    name = slug.replace("-", " ").title()
    return cors(jsonify({"meta": {
        "id": f"rido:{slug}", "type": type_, "name": name,
        "description": f"Rido: {name}",
    }}))


@app.route("/stream/<type_>/<path:id_>.json")
def stream(type_, id_):
    season = episode = None
    slug = id_[5:] if id_.startswith("rido:") else id_
    parts = slug.split(":")
    if len(parts) >= 3 and parts[-2].isdigit() and parts[-1].isdigit():
        season, episode = int(parts[-2]), int(parts[-1])
        slug = ":".join(parts[:-2])
    streams = []
    if slug and not slug.startswith("tt") and not slug.startswith("tmdb"):
        streams = resolve_streams(slug, season, episode)
    return cors(jsonify({"streams": streams}))


@app.route("/health")
def health():
    return cors(jsonify({"ok": True, "version": VERSION, "base": ACTIVE_BASE}))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=51823, debug=False)

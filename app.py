#!/usr/bin/env python3
"""RidoMovies Nuvio addon v1.1.0 — HTML catalog + Closeload embed. Port 51823."""
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
VERSION = "1.1.0"
ADDON_NAME = "Rido"
BASE = "https://ridomovie.to"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CACHE: dict = {}
CACHE_TTL = 600


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


def http_get(url: str, referer: str | None = None) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer or (BASE + "/home-rd1"),
    }
    kwargs = {"headers": headers, "timeout": 25}
    try:
        r = cf_requests.get(url, impersonate="chrome131", **kwargs)
    except TypeError:
        r = cf_requests.get(url, **kwargs)
    r.raise_for_status()
    return r.text


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


def decode_closeload(embed_url: str) -> Optional[str]:
    html = http_get(embed_url, referer=BASE + "/")
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


def decode_ridoo(embed_url: str) -> Optional[str]:
    html = http_get(embed_url, referer=BASE + "/")
    m = re.search(r'file\s*:\s*"([^"]+)"', html) or re.search(r'file:"([^"]+)"', html)
    if m and m.group(1).startswith("http"):
        return m.group(1)
    m3 = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', html)
    return m3.group(1) if m3 else None


def scrape_list(path: str) -> list:
    html = http_get(BASE + path)
    items = []
    seen = set()
    for m in re.finditer(
        r'href=["\']/(movie/[^"\']+|tv/[^"\']+)["\']',
        html,
        re.I,
    ):
        slug = m.group(1).strip("/")
        if slug in seen:
            continue
        seen.add(slug)
        name = slug.split("/")[-1].replace("-", " ").title()
        year = ""
        ym = re.search(r"(19|20)\d{2}", slug)
        if ym:
            year = ym.group(0)
        typ = "series" if slug.startswith("tv/") else "movie"
        items.append(
            {
                "id": f"rido:{slug}",
                "type": typ,
                "name": name,
                "releaseInfo": year,
                "poster": None,
            }
        )
    return items


def extract_embeds_from_page(slug: str) -> list:
    """slug like movie/xxx or tv/xxx"""
    html = http_get(BASE + "/" + slug.lstrip("/"))
    embeds = []
    for pat in [
        r'data-src=["\'](https?://[^"\']+)["\']',
        r'iframe[^>]+src=["\'](https?://[^"\']+)["\']',
        r'(https?://closeload\.top/[^"\'\s<>]+)',
        r'(https?://[^"\'\s]*ridoo[^"\'\s]*)',
    ]:
        for u in re.findall(pat, html, re.I):
            if u not in embeds and u.startswith("http"):
                embeds.append(u)
    return embeds


def resolve_streams(slug: str) -> list:
    streams = []
    for emb in extract_embeds_from_page(slug):
        host = (urlparse(emb).hostname or "").lower()
        try:
            if "closeload" in host:
                m3u8 = decode_closeload(emb)
                if m3u8:
                    streams.append(
                        {
                            "name": "Rido",
                            "title": "Closeload\nFHD",
                            "url": m3u8,
                            "behaviorHints": {
                                "notWebReady": True,
                                "bingeGroup": f"rido-cl-{slug}",
                                "proxyHeaders": {
                                    "request": {
                                        "Referer": "https://closeload.top/",
                                        "Origin": "https://closeload.top",
                                        "User-Agent": UA,
                                    }
                                },
                            },
                        }
                    )
            elif "ridoo" in host or "rapid" in host:
                m3u8 = decode_ridoo(emb)
                if m3u8:
                    streams.append(
                        {
                            "name": "Rido",
                            "title": "RidoRapid\nFHD",
                            "url": m3u8,
                            "behaviorHints": {
                                "notWebReady": True,
                                "bingeGroup": f"rido-rp-{slug}",
                                "proxyHeaders": {
                                    "request": {
                                        "Referer": BASE + "/",
                                        "User-Agent": UA,
                                    }
                                },
                            },
                        }
                    )
        except Exception as e:
            app.logger.warning("embed fail %s: %s", emb, e)
    return streams


@app.route("/")
@app.route("/manifest.json")
def manifest():
    return cors(
        jsonify(
            {
                "id": "community.rido.streams",
                "version": VERSION,
                "name": ADDON_NAME,
                "description": "RidoMovies — Closeload + RidoRapid (HTML)",
                "resources": ["catalog", "meta", "stream"],
                "types": ["movie", "series"],
                "idPrefixes": ["rido:"],
                "catalogs": [
                    {
                        "type": "movie",
                        "id": "rido-movies",
                        "name": "Rido Movies",
                    },
                    {
                        "type": "series",
                        "id": "rido-series",
                        "name": "Rido Series",
                    },
                ],
            }
        )
    )


@app.route("/catalog/<type_>/<id_>.json")
@app.route("/catalog/<type_>/<id_>/skip=<int:skip>.json")
def catalog(type_, id_, skip=0):
    def _load():
        if id_ == "rido-series" or type_ == "series":
            return scrape_list("/tv-series-rd1")
        return scrape_list("/movies-rd1")

    items = cached(f"cat:{id_}", _load, 300)
    metas = items[skip : skip + 100]
    return cors(jsonify({"metas": metas}))


@app.route("/meta/<type_>/<path:id_>.json")
def meta(type_, id_):
    slug = id_[5:] if id_.startswith("rido:") else id_
    name = slug.split("/")[-1].replace("-", " ").title()
    return cors(
        jsonify(
            {
                "meta": {
                    "id": f"rido:{slug}",
                    "type": type_,
                    "name": name,
                    "description": f"Rido: {name}",
                }
            }
        )
    )


@app.route("/stream/<type_>/<path:id_>.json")
def stream(type_, id_):
    slug = id_[5:] if id_.startswith("rido:") else id_
    # strip season/episode suffix if present
    parts = slug.split(":")
    if len(parts) >= 3 and parts[-2].isdigit() and parts[-1].isdigit():
        slug = ":".join(parts[:-2])
    streams = []
    if slug and ("/" in slug or slug.startswith("movie") or slug.startswith("tv")):
        streams = cached(f"stream:{slug}", lambda: resolve_streams(slug), 300)
    return cors(jsonify({"streams": streams}))


@app.route("/health")
def health():
    return cors(jsonify({"ok": True, "version": VERSION, "base": BASE}))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=51823, debug=False)

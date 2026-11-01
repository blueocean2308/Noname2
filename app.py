#!/usr/bin/env python3
"""BingeBang addon for Nuvio/Stremio — port 51823"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import quote, urljoin

from flask import Flask, Response, request
from flask_cors import CORS

import requests

APP_NAME = "BingeBang"
APP_ID = "org.nuvio.bingebang"
VERSION = "1.0.0"
BASE = "https://bingebang.st"
IMG = "https://image.tmdb.org/t/p"
PORT = int(os.environ.get("PORT", "51823"))
FLARESOLVERR = os.environ.get("FLARESOLVERR", "http://127.0.0.1:8191/v1")
CRYPTO_JS = os.environ.get(
    "BB_CRYPTO_JS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bb_crypto.js"),
)

UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)

app = Flask(__name__)
CORS(app)

_ticket_cache: dict[str, tuple[float, str]] = {}
TICKET_TTL = 25 * 60


def j(data: Any, code: int = 200) -> Response:
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=code,
        mimetype="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def http_get(url: str, headers: dict | None = None, timeout: int = 20) -> requests.Response:
    h = {"User-Agent": UA, "Accept": "application/json,text/html,*/*"}
    if headers:
        h.update(headers)
    return requests.get(url, headers=h, timeout=timeout)


def api_get(path: str, params: dict | None = None) -> Any:
    url = urljoin(BASE + "/", path.lstrip("/"))
    if params:
        q = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        url = f"{url}?{q}"
    r = http_get(url)
    r.raise_for_status()
    return r.json()


def poster_url(path: str | None, size: str = "w500") -> str | None:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"{IMG}/{size}{path}"


def item_to_meta(item: dict, type_: str | None = None) -> dict:
    mt = type_ or item.get("media_type") or "movie"
    st_type = "series" if mt in ("tv", "series") else "movie"
    bid = item.get("id")
    title = item.get("title") or item.get("name") or item.get("original_title") or "Unknown"
    year = item.get("year")
    if not year:
        rd = item.get("release_date") or item.get("first_air_date") or ""
        year = rd[:4] if rd else None
    meta = {
        "id": f"bb:{bid}" if bid else title,
        "type": st_type,
        "name": title,
        "poster": poster_url(item.get("poster_path")),
        "background": poster_url(item.get("backdrop_path"), "w1280"),
        "description": item.get("overview") or "",
        "releaseInfo": str(year) if year else "",
        "imdbRating": str(item.get("vote_average")) if item.get("vote_average") else None,
    }
    play = item.get("play_url") or item.get("url")
    if play:
        meta["bingebang_play"] = play
    return {k: v for k, v in meta.items() if v is not None}


def extract_ticket_from_html(html: str) -> str | None:
    m = re.search(r"__BB_PLAYER__\s*=\s*(\{.*?\})\s*;", html, re.S)
    if not m:
        m = re.search(r"window\.__BB_PLAYER__\s*=\s*(\{.*?\})\s*;", html, re.S)
    if m:
        try:
            return json.loads(m.group(1)).get("ticket")
        except Exception:
            pass
    m = re.search(r'"ticket"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else None


def get_ticket_flaresolverr(play_path: str) -> str | None:
    play_url = urljoin(BASE, play_path)
    try:
        r = requests.post(
            FLARESOLVERR,
            json={"cmd": "request.get", "url": play_url, "maxTimeout": 60000},
            timeout=70,
        )
        html = (r.json().get("solution") or {}).get("response") or ""
        return extract_ticket_from_html(html)
    except Exception as e:
        print("flaresolverr error:", e)
        return None


def get_ticket_direct(play_path: str) -> str | None:
    try:
        r = http_get(urljoin(BASE, play_path), headers={"Referer": BASE + "/"})
        if "Quick check" in r.text or "turnstile" in r.text.lower():
            return None
        return extract_ticket_from_html(r.text)
    except Exception as e:
        print("ticket direct error:", e)
        return None


def get_ticket(play_path: str) -> str | None:
    now = time.time()
    cached = _ticket_cache.get(play_path)
    if cached and now - cached[0] < TICKET_TTL:
        return cached[1]
    ticket = get_ticket_direct(play_path) or get_ticket_flaresolverr(play_path)
    if ticket:
        _ticket_cache[play_path] = (now, ticket)
    return ticket


def decrypt_sources(ticket: str, enc_body: str) -> dict | None:
    if not os.path.isfile(CRYPTO_JS):
        print("missing bb_crypto.js at", CRYPTO_JS)
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(enc_body)
        path = f.name
    try:
        p = subprocess.run(
            ["node", CRYPTO_JS, ticket, path],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if p.returncode != 0:
            print("decrypt stderr:", p.stderr[:500])
            return None
        return json.loads(p.stdout)
    except Exception as e:
        print("decrypt error:", e)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def fetch_sources_raw(ticket: str, play_path: str) -> str | None:
    r = http_get(
        f"{BASE}/api/player/sources",
        headers={
            "X-BB-Player": "1",
            "X-BB-Ticket": ticket,
            "Referer": urljoin(BASE, play_path),
            "Accept": "application/json,*/*",
        },
    )
    if r.status_code != 200:
        print("sources status", r.status_code, r.text[:200])
        return None
    return r.text


def resolve_src(ticket: str, play_path: str, source_url: str) -> dict | None:
    url = f"{BASE}/api/player/resolve?src={quote(source_url, safe='')}"
    r = http_get(
        url,
        headers={
            "X-BB-Player": "1",
            "X-BB-Ticket": ticket,
            "Referer": urljoin(BASE, play_path),
            "Accept": "application/json,*/*",
        },
    )
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    if data.get("ok") and data.get("url"):
        return data
    return None


def get_streams_for_play(play_path: str) -> list[dict]:
    ticket = get_ticket(play_path)
    if not ticket:
        print("no ticket for", play_path)
        return []
    raw = fetch_sources_raw(ticket, play_path)
    if not raw:
        return []
    try:
        raw_j = json.loads(raw)
    except Exception:
        return []
    data = decrypt_sources(ticket, raw) if "enc" in raw_j else raw_j
    if not data or not data.get("ok"):
        return []
    streams: list[dict] = []
    for s in data.get("servers") or []:
        src = s.get("source_url")
        if not src:
            continue
        resolved = resolve_src(ticket, play_path, src)
        if not resolved:
            continue
        label = s.get("label") or "Server"
        quality = s.get("quality") or resolved.get("quality") or ""
        title = f"{label} · {quality}" if quality else label
        streams.append(
            {
                "name": "BingeBang",
                "title": title,
                "url": resolved["url"],
                "behaviorHints": {
                    "notWebReady": False,
                    "bingeGroup": f"bingebang-{play_path}",
                    "proxyHeaders": {
                        "request": {
                            "User-Agent": UA,
                            "Referer": urljoin(BASE, play_path),
                        }
                    },
                },
            }
        )
    return streams


def search_bb(keyword: str) -> list[dict]:
    try:
        data = api_get("/api/search/multi", {"query": keyword})
        return data.get("results") or []
    except Exception as e:
        print("search error", e)
        return []


def cinemeta_name(imdb_id: str) -> str | None:
    for t in ("movie", "series"):
        try:
            r = http_get(f"https://v3-cinemeta.strem.io/meta/{t}/{imdb_id}.json", timeout=12)
            if r.status_code == 200:
                meta = r.json().get("meta") or {}
                name = meta.get("name")
                year = (meta.get("releaseInfo") or meta.get("year") or "")[:4]
                if name and year:
                    return f"{name} {year}"
                if name:
                    return name
        except Exception:
            pass
    return None


def resolve_play_path(video_id: str) -> str | None:
    if video_id.startswith("bb:"):
        rest = video_id[3:]
        if rest.isdigit():
            for kind in ("movie", "tv"):
                try:
                    data = api_get(f"/api/{kind}/{rest}")
                    return data.get("play_url")
                except Exception:
                    continue
            return None
        if rest.startswith("/"):
            return rest
        return f"/movie/play/{rest}"

    imdb = video_id.split(":")[0]
    if not imdb.startswith("tt"):
        return None
    name = cinemeta_name(imdb)
    if not name:
        return None
    results = search_bb(name)
    for it in results:
        if it.get("imdb_id") == imdb or it.get("imdb") == imdb:
            return it.get("play_url")
    for it in results:
        if it.get("play_url"):
            return it["play_url"]
    return None


@app.route("/manifest.json")
def manifest():
    return j(
        {
            "id": APP_ID,
            "version": VERSION,
            "name": APP_NAME,
            "description": "BingeBang streams for Nuvio",
            "resources": ["catalog", "meta", "stream"],
            "types": ["movie", "series"],
            "idPrefixes": ["tt", "bb"],
            "catalogs": [
                {
                    "type": "movie",
                    "id": "bb_search",
                    "name": "BingeBang Search",
                    "extra": [{"name": "search", "isRequired": True}],
                },
                {
                    "type": "movie",
                    "id": "bb_movie",
                    "name": "BingeBang Movies",
                    "extra": [{"name": "skip"}],
                },
                {
                    "type": "series",
                    "id": "bb_series",
                    "name": "BingeBang Series",
                    "extra": [{"name": "skip"}],
                },
            ],
        }
    )


@app.route("/catalog/<type_>/<id_>.json")
@app.route("/catalog/<type_>/<id_>/search=<query>.json")
def catalog(type_: str, id_: str, query: str | None = None):
    q = query or request.args.get("search")
    metas: list[dict] = []
    try:
        if q:
            for it in search_bb(q):
                metas.append(item_to_meta(it))
        elif id_ == "bb_movie":
            data = api_get("/api/list/movie", {"page": "1"})
            for it in data.get("results") or data.get("items") or []:
                metas.append(item_to_meta(it, "movie"))
            if not metas:
                for it in search_bb("2026"):
                    if it.get("media_type") != "tv":
                        metas.append(item_to_meta(it, "movie"))
        elif id_ == "bb_series":
            data = api_get("/api/list/tv", {"page": "1"})
            for it in data.get("results") or data.get("items") or []:
                metas.append(item_to_meta(it, "series"))
            if not metas:
                for it in search_bb("2025"):
                    if it.get("media_type") == "tv":
                        metas.append(item_to_meta(it, "series"))
    except Exception as e:
        print("catalog error", e)
    return j({"metas": metas})


@app.route("/meta/<type_>/<path:id_>.json")
def meta(type_: str, id_: str):
    id_ = id_.replace(".json", "")
    try:
        if id_.startswith("bb:") and id_[3:].isdigit():
            bid = id_[3:]
            for kind in ("movie", "tv"):
                try:
                    data = api_get(f"/api/{kind}/{bid}")
                    return j({"meta": item_to_meta(data, type_)})
                except Exception:
                    continue
        return j({"meta": {"id": id_, "type": type_, "name": id_}})
    except Exception as e:
        print("meta error", e)
        return j({"meta": {}})


@app.route("/stream/<type_>/<path:id_>.json")
def stream(type_: str, id_: str):
    id_ = id_.replace(".json", "")
    play = resolve_play_path(id_)
    if not play:
        print("no play path for", id_)
        return j({"streams": []})
    if type_ == "series" and id_.count(":") >= 2:
        parts = id_.split(":")
        try:
            season, episode = int(parts[-2]), int(parts[-1])
            sep = "&" if "?" in play else "?"
            play = f"{play}{sep}season={season}&episode={episode}"
        except Exception:
            pass
    return j({"streams": get_streams_for_play(play)})


@app.route("/")
def root():
    return j({"name": APP_NAME, "version": VERSION, "manifest": "/manifest.json"})


if __name__ == "__main__":
    print(f"{APP_NAME} {VERSION} on 0.0.0.0:{PORT}")
    print("bb_crypto.js:", CRYPTO_JS, "exists=", os.path.isfile(CRYPTO_JS))
    app.run(host="0.0.0.0", port=PORT, threaded=True)

#!/usr/bin/env python3
"""Koddo native addon for Nuvio/Stremio — catalog + 1080p streams only."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

from flask import Flask, Response, jsonify, request

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except Exception:
    import requests as cffi_requests
    HAS_CFFI = False

APP_NAME = "Koddo"
APP_ID = "org.nuvio.koddo"
VERSION = "1.0.0"
PORT = int(os.environ.get("PORT", "51823"))
KODDO = "https://koddo.to"
CDN = "https://useamass.cyou"
MIN_HEIGHT = 1080
COOKIE = os.environ.get("KODDO_COOKIE", "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

CACHE_TTL = 600
_cache: Dict[str, Tuple[float, Any]] = {}

app = Flask(__name__)


def _cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,HEAD,OPTIONS"
    return resp


@app.after_request
def after(resp: Response) -> Response:
    return _cors(resp)


def cache_get(key: str) -> Any:
    item = _cache.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return val


def cache_set(key: str, val: Any) -> Any:
    _cache[key] = (time.time(), val)
    return val


def http_get(url: str, accept: str = "application/json", extra: Optional[dict] = None) -> Tuple[int, Any]:
    headers = {
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
        "Referer": f"{KODDO}/",
        "Origin": KODDO,
        "Cache-Control": "no-cache",
    }
    if COOKIE:
        headers["Cookie"] = COOKIE
    if extra:
        headers.update(extra)
    try:
        if HAS_CFFI:
            r = cffi_requests.get(url, headers=headers, timeout=25, impersonate="chrome131")
        else:
            r = cffi_requests.get(url, headers=headers, timeout=25)
        ct = (r.headers.get("content-type") or "").lower()
        if "json" in ct or accept.startswith("application/json"):
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, r.text
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)


def search_koddo(query: str) -> List[dict]:
    q = (query or "").strip()
    if not q:
        return []
    key = f"search:{q.lower()}"
    hit = cache_get(key)
    if hit is not None:
        return hit
    st, data = http_get(f"{KODDO}/v1/search/{quote(q)}?lang=en")
    results = []
    if st == 200 and isinstance(data, dict):
        results = data.get("results") or []
    return cache_set(key, results)


def get_title(kind: str, kid: str) -> Optional[dict]:
    key = f"title:{kind}:{kid}"
    hit = cache_get(key)
    if hit is not None:
        return hit
    path = "movies" if kind == "movie" else "series"
    st, data = http_get(f"{KODDO}/v1/{path}/{kid}?lang=en")
    if st == 200 and isinstance(data, dict) and data.get("id"):
        return cache_set(key, data)
    return None


def provider_items(slug: str = "netflix") -> List[dict]:
    key = f"prov:{slug}"
    hit = cache_get(key)
    if hit is not None:
        return hit
    st, data = http_get(f"{KODDO}/v1/provider/{slug}?kind=iconic&lang=en")
    items = []
    if st == 200 and isinstance(data, dict):
        items = data.get("items") or data.get("results") or []
    elif st == 200 and isinstance(data, list):
        items = data
    return cache_set(key, items)


def meta_from_item(item: dict, type_hint: Optional[str] = None) -> dict:
    kind = item.get("kind") or type_hint or "movie"
    stype = "series" if kind == "series" else "movie"
    kid = item.get("id") or ""
    year = item.get("year")
    meta = {
        "id": f"koddo:{stype}:{kid}",
        "type": stype,
        "name": item.get("name") or kid,
        "poster": item.get("poster"),
        "background": item.get("backdrop") or item.get("poster"),
        "logo": item.get("logo"),
        "description": item.get("description") or "",
        "releaseInfo": str(year) if year else "",
        "imdbRating": str(item.get("rating")) if item.get("rating") else None,
        "genres": item.get("tags") or [],
    }
    return {k: v for k, v in meta.items() if v is not None}


def build_series_videos(title: dict) -> List[dict]:
    videos = []
    kid = title.get("id")
    seasons = title.get("seasons") or []
    prefetch = title.get("prefetch_episodes") or []
    pref_season = title.get("prefetch_season") or 1
    pref_map = {int(e.get("episode") or 0): e for e in prefetch if isinstance(e, dict)}

    for s in seasons:
        if not isinstance(s, dict):
            continue
        sn = int(s.get("season") or 0)
        count = int(s.get("released_episode_count") or s.get("episode_count") or 0)
        for ep in range(1, max(count, 0) + 1):
            info = pref_map.get(ep) if sn == int(pref_season) else None
            title_ep = (info or {}).get("name") or f"Tap {ep}"
            videos.append(
                {
                    "id": f"koddo:series:{kid}:{sn}:{ep}",
                    "title": title_ep,
                    "season": sn,
                    "episode": ep,
                    "released": (info or {}).get("air_date") or "",
                    "thumbnail": (info or {}).get("still"),
                    "overview": (info or {}).get("description") or "",
                }
            )
    return videos


def parse_stream_id(video_id: str) -> Optional[Tuple[str, str, int, int]]:
    video_id = unquote(video_id or "").replace(".json", "")
    if video_id.startswith("koddo:"):
        parts = video_id.split(":")
        if len(parts) >= 3 and parts[1] == "movie":
            return "movie", parts[2], 0, 0
        if len(parts) >= 5 and parts[1] == "series":
            try:
                return "series", parts[2], int(parts[3]), int(parts[4])
            except ValueError:
                return None
        if len(parts) == 3 and parts[1] == "series":
            return "series", parts[2], 1, 1
    return None


def player_grant(token: str) -> Optional[str]:
    key = f"grant:{token}"
    hit = cache_get(key)
    if hit is not None:
        return hit
    st, data = http_get(f"{KODDO}/api/player-gate/status?t={quote(token, safe='')}")
    if st == 200 and isinstance(data, dict) and data.get("ok") and data.get("grant"):
        return cache_set(key, data["grant"])
    return None


def fetch_sources(grant: str) -> List[dict]:
    key = f"src:{grant}"
    hit = cache_get(key)
    if hit is not None:
        return hit
    url = f"{CDN}/c/v2/{grant}/master.m3u8?cv=14"
    st, data = http_get(url, accept="application/json")
    sources = []
    if st == 200 and isinstance(data, dict):
        sources = data.get("sources") or []
    return cache_set(key, sources)


def filter_1080(sources: List[dict]) -> List[dict]:
    out = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        url = s.get("url") or ""
        if not url:
            continue
        max_q = int(s.get("maxQuality") or 0)
        height = 0
        q = s.get("quality")
        if isinstance(q, dict):
            height = int(q.get("height") or 0)
        quals = s.get("qualities") or []
        if isinstance(quals, list) and quals:
            try:
                height = max(height, max(int(x) for x in quals if isinstance(x, int) or str(x).isdigit()))
            except Exception:
                pass
        best = max(max_q, height)
        if best and best < MIN_HEIGHT:
            continue
        out.append(s)
    return out


def streams_for_token(token: str, label_base: str) -> List[dict]:
    grant = player_grant(token)
    if not grant:
        return []
    sources = filter_1080(fetch_sources(grant))
    streams = []
    for s in sources:
        url = s.get("url")
        if not url:
            continue
        height = 0
        q = s.get("quality")
        if isinstance(q, dict):
            height = int(q.get("height") or 0)
        max_q = int(s.get("maxQuality") or height or 1080)
        qlabel = f"{max_q}p" if max_q else "1080p"
        langs = s.get("languages") or []
        lang = ",".join(langs) if langs else ""
        name = s.get("label") or s.get("id") or "Koddo"
        title_lines = [label_base, f"{name} · {qlabel}"]
        if lang:
            title_lines.append(lang)
        streams.append(
            {
                "name": f"Koddo · {qlabel}",
                "title": "\n".join(title_lines),
                "url": url,
                "behaviorHints": {
                    "notWebReady": False,
                    "bingeGroup": f"koddo-{token}",
                    "proxyHeaders": {
                        "request": {
                            "Referer": f"{KODDO}/",
                            "Origin": KODDO,
                            "User-Agent": UA,
                        }
                    },
                },
            }
        )
    return streams


def cinemeta_name(stype: str, imdb: str) -> str:
    st, data = http_get(f"https://v3-cinemeta.strem.io/meta/{stype}/{imdb}.json")
    if st == 200 and isinstance(data, dict):
        meta = data.get("meta") or {}
        return meta.get("name") or meta.get("originalName") or ""
    return ""


def find_koddo_by_name(name: str, stype: str, year: Optional[int] = None) -> Optional[str]:
    results = search_koddo(name)
    kind = "series" if stype == "series" else "movie"
    exact = []
    soft = []
    nl = name.lower().strip()
    for r in results:
        if (r.get("kind") or "") != kind:
            continue
        rn = (r.get("name") or "").lower().strip()
        if rn == nl:
            exact.append(r)
        elif nl in rn or rn in nl:
            soft.append(r)
    pool = exact or soft
    if year and pool:
        for r in pool:
            if str(r.get("year")) == str(year):
                return r.get("id")
    return pool[0].get("id") if pool else None


@app.route("/")
@app.route("/manifest.json")
def manifest():
    return jsonify(
        {
            "id": APP_ID,
            "version": VERSION,
            "name": APP_NAME,
            "description": "Koddo native 1080p+ (Solace). Set KODDO_COOKIE after Turnstile if gate blocks VPS.",
            "logo": "https://koddo.to/android-chrome-192x192-v2.png",
            "resources": ["catalog", "meta", "stream"],
            "types": ["movie", "series"],
            "idPrefixes": ["koddo:", "tt"],
            "catalogs": [
                {
                    "type": "movie",
                    "id": "koddo-movie",
                    "name": "Koddo Movies",
                    "extra": [{"name": "search", "isRequired": False}, {"name": "skip"}],
                },
                {
                    "type": "series",
                    "id": "koddo-series",
                    "name": "Koddo Series",
                    "extra": [{"name": "search", "isRequired": False}, {"name": "skip"}],
                },
            ],
            "behaviorHints": {"configurable": False, "configurationRequired": False},
        }
    )


@app.route("/catalog/<stype>/<cid>.json")
@app.route("/catalog/<stype>/<cid>/<path:extra>.json")
def catalog(stype: str, cid: str, extra: str = ""):
    search_q = ""
    if extra:
        m = re.search(r"search=([^&/]+)", extra)
        if m:
            search_q = unquote(m.group(1))

    metas: List[dict] = []
    if search_q:
        for r in search_koddo(search_q):
            kind = r.get("kind")
            if stype == "movie" and kind != "movie":
                continue
            if stype == "series" and kind != "series":
                continue
            metas.append(meta_from_item(r, stype))
    else:
        for r in provider_items("netflix"):
            if not isinstance(r, dict) or not r.get("id"):
                continue
            kind = r.get("kind") or r.get("type")
            if stype == "movie" and kind == "series":
                continue
            if stype == "series" and kind == "movie":
                continue
            r = dict(r)
            r["kind"] = "series" if stype == "series" else "movie"
            metas.append(meta_from_item(r, stype))
        if not metas:
            q = "matrix" if stype == "movie" else "breaking"
            for r in search_koddo(q):
                kind = r.get("kind")
                if stype == "movie" and kind == "movie":
                    metas.append(meta_from_item(r))
                if stype == "series" and kind == "series":
                    metas.append(meta_from_item(r))

    return jsonify({"metas": metas[:60]})


@app.route("/meta/<stype>/<path:mid>.json")
def meta(stype: str, mid: str):
    mid = unquote(mid).replace(".json", "")
    kid = mid
    if mid.startswith("koddo:"):
        parts = mid.split(":")
        if len(parts) >= 3:
            stype = parts[1]
            kid = parts[2]
    kind = "series" if stype == "series" else "movie"
    title = get_title(kind, kid)
    if not title:
        return jsonify({"meta": {}})
    m = meta_from_item(title, kind)
    m["id"] = f"koddo:{kind}:{kid}"
    if kind == "series":
        m["videos"] = build_series_videos(title)
    return jsonify({"meta": m})


@app.route("/stream/<stype>/<path:sid>.json")
def stream(stype: str, sid: str):
    sid = unquote(sid).replace(".json", "")
    streams: List[dict] = []

    parsed = parse_stream_id(sid)
    if parsed:
        ktype, kid, season, episode = parsed
        if ktype == "movie":
            label = kid
            title = get_title("movie", kid)
            if title:
                label = f"{title.get('name') or kid}"
                if title.get("year"):
                    label += f" ({title['year']})"
            streams = streams_for_token(kid, label)
        else:
            token = f"{kid}.{season}.{episode}"
            label = f"Tap {episode}"
            title = get_title("series", kid)
            if title:
                label = f"{title.get('name') or kid} · Tap {episode}"
            streams = streams_for_token(token, label)
        return jsonify({"streams": streams})

    if sid.startswith("tt") or re.match(r"^tt\d+", sid):
        imdb = sid.split(":")[0]
        season = episode = 0
        if stype == "series" and sid.count(":") >= 2:
            parts = sid.split(":")
            try:
                season, episode = int(parts[1]), int(parts[2])
            except ValueError:
                pass
        name = cinemeta_name(stype, imdb)
        if name:
            kid = find_koddo_by_name(name, stype)
            if kid:
                if stype == "movie":
                    streams = streams_for_token(kid, name)
                else:
                    s, e = season or 1, episode or 1
                    streams = streams_for_token(f"{kid}.{s}.{e}", f"{name} · Tap {e}")

    return jsonify({"streams": streams})


@app.route("/health")
def health():
    return jsonify({"ok": True, "version": VERSION, "cffi": HAS_CFFI, "cookie_set": bool(COOKIE)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)

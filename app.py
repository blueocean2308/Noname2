#!/usr/bin/env python3
"""67Movies / Ravenflix addon for Nuvio - VPS + curl_cffi"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional
from urllib.parse import quote

from flask import Flask, Response, request
from curl_cffi import requests as cffi_requests

app = Flask(__name__)

MANIFEST = {
    "id": "org.nuvio.ravenflix",
    "version": "1.0.1",
    "name": "67Movies",
    "description": "Ravenflix / Vidlove (MovieBox) via VPS",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt", "tmdb:"],
    "catalogs": [],
}

API = "https://api.ravenflix.st"
REFERER = "https://player.vidlove.st/"
ORIGIN = "https://player.vidlove.st"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TMDB_KEY = "a46c50a0ccb1bafe2b15665df7fad7e1"
SOURCES = ["moviebox2", "moviebox", "megaknight", "dcloud", "tcloud", "vidapi"]
CACHE_TTL = 600
_cache: dict[str, tuple[float, Any]] = {}
_session = cffi_requests.Session()


def cors_json(data: Any, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False)
    return Response(
        body,
        status=status,
        mimetype="application/json",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Cache-Control": "no-store",
        },
    )


def cache_get(key: str) -> Optional[Any]:
    item = _cache.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return val


def cache_set(key: str, val: Any) -> None:
    _cache[key] = (time.time(), val)
    if len(_cache) > 500:
        oldest = sorted(_cache.items(), key=lambda x: x[1][0])[:100]
        for k, _ in oldest:
            _cache.pop(k, None)


def http_get(url: str, headers: Optional[dict] = None) -> Optional[Any]:
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": ORIGIN,
        "Referer": REFERER,
    }
    if headers:
        h.update(headers)
    try:
        r = _session.get(url, headers=h, impersonate="chrome131", timeout=25)
        if r.status_code != 200:
            return None
        ct = (r.headers.get("content-type") or "").lower()
        if "json" in ct or r.text[:1] in "{[":
            return r.json()
        return r.text
    except Exception:
        return None


def parse_id(raw: str) -> dict:
    raw = (raw or "").replace(".json", "")
    parts = raw.split(":")
    out: dict[str, Any] = {
        "imdb": None,
        "tmdb": None,
        "season": None,
        "episode": None,
        "series": False,
    }
    if parts[0] == "tmdb":
        out["tmdb"] = parts[1]
        if len(parts) >= 4:
            out["season"] = int(parts[2])
            out["episode"] = int(parts[3])
            out["series"] = True
    elif parts[0].startswith("tt"):
        out["imdb"] = parts[0]
        if len(parts) >= 3:
            out["season"] = int(parts[1])
            out["episode"] = int(parts[2])
            out["series"] = True
    return out


def imdb_to_tmdb(imdb: str) -> Optional[dict]:
    ck = f"imdb:{imdb}"
    hit = cache_get(ck)
    if hit is not None:
        return hit
    url = (
        f"https://api.themoviedb.org/3/find/{imdb}"
        f"?api_key={TMDB_KEY}&external_source=imdb_id"
    )
    data = http_get(url, headers={"Origin": None, "Referer": None})  # type: ignore
    # TMDB không cần origin ravenflix
    try:
        r = _session.get(
            url,
            headers={"User-Agent": UA, "Accept": "application/json"},
            impersonate="chrome131",
            timeout=15,
        )
        data = r.json() if r.status_code == 200 else None
    except Exception:
        data = None
    result = None
    if data:
        if data.get("movie_results"):
            result = {"tmdb": str(data["movie_results"][0]["id"]), "type": "movie"}
        elif data.get("tv_results"):
            result = {"tmdb": str(data["tv_results"][0]["id"]), "type": "series"}
    cache_set(ck, result)
    return result


def fetch_source(
    tmdb: str, series: bool, season: Optional[int], episode: Optional[int], source: str
) -> Optional[dict]:
    if series:
        path = (
            f"/tv?id={tmdb}&season={season}&episode={episode}"
            f"&mode=json&sources={source}&hevc=1&vh=1"
        )
    else:
        path = f"/movie?id={tmdb}&mode=json&sources={source}&hevc=1&vh=1"
    ck = f"src:{path}"
    hit = cache_get(ck)
    if hit is not None:
        return hit
    data = http_get(API + path)
    src = None
    if isinstance(data, dict) and data.get("source"):
        src = data["source"]
    cache_set(ck, src)
    return src


def streams_from_source(src: dict, binge: str) -> list[dict]:
    streams: list[dict] = []
    if not isinstance(src, dict):
        return streams

    label = src.get("label") or src.get("source") or "Ravenflix"
    proxy = {
        "notWebReady": True,
        "bingeGroup": binge,
        "proxyHeaders": {
            "request": {
                "Referer": REFERER,
                "Origin": ORIGIN,
                "User-Agent": UA,
            }
        },
    }

    qualities = src.get("qualities")
    if isinstance(qualities, list):
        for q in qualities:
            if not isinstance(q, dict) or not q.get("url"):
                continue
            quality = str(q.get("quality") or "HD").upper()
            codec = q.get("codec") or ""
            qtype = q.get("type") or "mp4"
            title = f"{label} · {quality}"
            if codec:
                title += f" · {codec}"
            title += f"\n{qtype}"
            streams.append(
                {
                    "name": "67Movies",
                    "title": title,
                    "url": q["url"],
                    "behaviorHints": {
                        **proxy,
                        "bingeGroup": f"{binge}-{quality}",
                    },
                }
            )

    # HLS / single url
    if not streams and src.get("url"):
        extra = ""
        if isinstance(src.get("manifest"), str) and "#EXTM3U" in src["manifest"]:
            extra = " · HLS"
        streams.append(
            {
                "name": "67Movies",
                "title": f"{label}{extra}\n{src.get('source') or 'stream'}",
                "url": src["url"],
                "behaviorHints": proxy,
            }
        )

    def rank(title: str) -> int:
        m = re.search(r"(\d{3,4})p", title, re.I)
        return int(m.group(1)) if m else 0

    streams.sort(key=lambda s: rank(s.get("title") or ""), reverse=True)
    return streams


@app.route("/manifest.json")
@app.route("/")
def manifest():
    return cors_json(MANIFEST)


@app.route("/stream/<type_>/<path:id_>")
def stream(type_: str, id_: str):
    if id_.endswith(".json"):
        id_ = id_[:-5]
    parsed = parse_id(id_)
    tmdb = parsed["tmdb"]
    series = type_ == "series" or parsed["series"]
    season = parsed["season"]
    episode = parsed["episode"]

    if not tmdb and parsed["imdb"]:
        mapped = imdb_to_tmdb(parsed["imdb"])
        if not mapped:
            return cors_json({"streams": []})
        tmdb = mapped["tmdb"]
        if mapped["type"] == "series":
            series = True

    if not tmdb:
        return cors_json({"streams": []})
    if series and (season is None or episode is None):
        return cors_json({"streams": []})

    binge = f"ravenflix-{tmdb}"
    if series:
        binge += f"-{season}-{episode}"

    all_streams: list[dict] = []
    seen: set[str] = set()

    for name in SOURCES:
        try:
            src = fetch_source(tmdb, series, season, episode, name)
        except Exception:
            src = None
        if not src:
            continue
        for s in streams_from_source(src, f"{binge}-{name}"):
            u = s.get("url")
            if not u or u in seen:
                continue
            seen.add(u)
            all_streams.append(s)

    return cors_json({"streams": all_streams})


@app.route("/health")
def health():
    return cors_json({"ok": True, "version": MANIFEST["version"]})


@app.after_request
def add_cors(resp: Response):
    resp.headers.setdefault("Access-Control-Allow-Origin", "*")
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=51823, threaded=True)

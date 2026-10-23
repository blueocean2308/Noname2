#!/usr/bin/env python3
"""Cno.to VPS addon — TMDB id stream (master HLS, prefer 1080p+).
1.0.0 — /api/s/{server}/movie|tv + Referer, no web scrape.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urljoin

from flask import Flask, Response, request
from curl_cffi import requests as cfreq

app = Flask(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
IMP = "chrome131"
CNO = "https://cno.to"
PUBLIC_HOST = "168.138.176.147:51823"
TMDB_KEY = "1adf1a2b5aece0ac5106302d3299f56f"
ADDON_VERSION = "1.0.0"

# Stream server ids to try (site rotates; 21 worked in probe)
SERVER_IDS = [21, 20, 22, 15, 10, 5, 1, 2, 25, 30]

API_CACHE: dict = {}
API_TTL = 300
TMDB_TTL = 86400


def S():
    s = cfreq.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def j(data, code=200):
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=code,
        mimetype="application/json",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
        },
    )


def public_root():
    host = request.headers.get("Host") or PUBLIC_HOST
    if host.startswith("127.") or "localhost" in host:
        host = PUBLIC_HOST
    return f"http://{host}"


def _prune(store, limit=400):
    now = time.time()
    for k in [k for k, v in list(store.items()) if v[1] <= now]:
        store.pop(k, None)
    if len(store) > limit:
        for k, _ in sorted(store.items(), key=lambda x: x[1][1])[: max(1, len(store) - limit)]:
            store.pop(k, None)


def cache_get(key):
    item = API_CACHE.get(key)
    if not item:
        return None
    if item[1] <= time.time():
        API_CACHE.pop(key, None)
        return None
    return item[0]


def cache_put(key, data, ttl):
    _prune(API_CACHE)
    API_CACHE[key] = (data, time.time() + ttl)


def cno_get(path: str, prefer_text: bool = False):
    url = path if path.startswith("http") else f"{CNO}{path}"
    r = S().get(
        url,
        headers={
            "Referer": f"{CNO}/",
            "Origin": CNO,
            "Accept": "application/json, application/vnd.apple.mpegurl, */*",
        },
        impersonate=IMP,
        timeout=20,
    )
    if prefer_text:
        return r.status_code, r.text
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


def tmdb_from_imdb(imdb_id: str):
    key = f"tmdb:{imdb_id}"
    hit = cache_get(key)
    if hit is not None:
        return hit
    try:
        r = S().get(
            f"https://api.themoviedb.org/3/find/{imdb_id}",
            params={
                "api_key": TMDB_KEY,
                "external_source": "imdb_id",
                "language": "en-US",
            },
            impersonate=IMP,
            timeout=12,
        )
        d = r.json()
        if d.get("movie_results"):
            x = d["movie_results"][0]
            res = ("movie", int(x["id"]), x.get("title") or "", (x.get("release_date") or "")[:4])
            cache_put(key, res, TMDB_TTL)
            return res
        if d.get("tv_results"):
            x = d["tv_results"][0]
            res = ("tv", int(x["id"]), x.get("name") or "", (x.get("first_air_date") or "")[:4])
            cache_put(key, res, TMDB_TTL)
            return res
    except Exception:
        pass
    cache_put(key, (None, None, None, None), 600)
    return None, None, None, None


def parse_master_m3u8(text: str, base: str):
    """Return list of {res, bandwidth, url} from master playlist, highest first."""
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            info = line
            res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", info)
            bw_m = re.search(r"BANDWIDTH=(\d+)", info)
            h = int(res_m.group(2)) if res_m else 0
            bw = int(bw_m.group(1)) if bw_m else 0
            if i + 1 < len(lines):
                u = lines[i + 1].strip()
                if u and not u.startswith("#"):
                    out.append(
                        {
                            "height": h,
                            "bandwidth": bw,
                            "url": urljoin(base, u),
                        }
                    )
            i += 2
            continue
        i += 1
    out.sort(key=lambda x: (x["height"], x["bandwidth"]), reverse=True)
    return out


def fetch_streams(kind: str, tmdb_id: int, season: int | None = None, episode: int | None = None):
    """kind: movie | tv. Returns list of variant dicts from first working server."""
    paths = []
    if kind == "movie":
        for sid in SERVER_IDS:
            paths.append(f"/api/s/{sid}/movie/{tmdb_id}?v=3")
    else:
        s, e = season or 1, episode or 1
        for sid in SERVER_IDS:
            paths.append(f"/api/s/{sid}/tv/{tmdb_id}/{s}/{e}?v=3")
            paths.append(f"/api/s/{sid}/tv/{tmdb_id}?season={s}&episode={e}&v=3")
            paths.append(f"/api/s/{sid}/series/{tmdb_id}/{s}/{e}?v=3")

    for path in paths:
        try:
            code, body = cno_get(path, prefer_text=True)
            if code != 200 or not isinstance(body, str):
                continue
            if not body.lstrip().startswith("#EXTM3U"):
                continue
            variants = parse_master_m3u8(body, CNO + path)
            if variants:
                return variants, path
        except Exception:
            continue
    return [], None


def quality_label(h: int) -> str:
    if h >= 2160:
        return "4K"
    if h >= 1080:
        return "1080p"
    if h >= 720:
        return "720p"
    if h >= 480:
        return "480p"
    return f"{h}p" if h else "HD"


@app.get("/manifest.json")
def manifest():
    return j(
        {
            "id": "org.nuvio.cno.vps",
            "version": ADDON_VERSION,
            "name": "Cno",
            "description": "Cno.to streams by TMDB id (1080p+)",
            "resources": ["stream"],
            "types": ["movie", "series"],
            "idPrefixes": ["tt"],
            "catalogs": [],
        }
    )


@app.get("/stream/<stype>/<sid>")
def stream(stype, sid):
    sid = sid.replace(".json", "")
    season = episode = None
    imdb = sid

    if stype == "series" and ":" in sid:
        parts = sid.split(":")
        imdb = parts[0]
        if len(parts) >= 3:
            season, episode = int(parts[1]), int(parts[2])

    if not imdb.startswith("tt"):
        return j({"streams": []})

    kind, tmdb_id, title, year = tmdb_from_imdb(imdb)
    if not tmdb_id:
        return j({"streams": []})

    # prefer path matching stype
    if stype == "movie":
        kind = "movie"
    elif stype == "series":
        kind = "tv"

    variants, used_path = fetch_streams(kind, tmdb_id, season, episode)
    # keep 720p+ (filter junk); prefer 1080+
    variants = [v for v in variants if v["height"] >= 720]
    if not variants:
        return j({"streams": []})

    streams = []
    name_base = title or imdb
    ybit = f" - {year}" if year else ""
    epbit = ""
    if season is not None and episode is not None:
        epbit = f" S{season:02d}E{episode:02d}"

    for v in variants:
        if v["height"] < 1080 and any(x["height"] >= 1080 for x in variants):
            # still allow 720 as fallback only if no 1080+
            continue
        q = quality_label(v["height"])
        streams.append(
            {
                "name": f"Cno {q}",
                "title": f"{name_base}{ybit}{epbit}\n{q}",
                "url": v["url"],
                "behaviorHints": {
                    "notWebReady": True,
                    "bingeGroup": f"cno-{tmdb_id}",
                    "proxyHeaders": {
                        "request": {
                            "Referer": f"{CNO}/",
                            "Origin": CNO,
                            "User-Agent": UA,
                        }
                    },
                },
            }
        )

    # if all filtered (only 720), take top 2
    if not streams and variants:
        for v in variants[:2]:
            q = quality_label(v["height"])
            streams.append(
                {
                    "name": f"Cno {q}",
                    "title": f"{name_base}{ybit}{epbit}\n{q}",
                    "url": v["url"],
                    "behaviorHints": {
                        "notWebReady": True,
                        "bingeGroup": f"cno-{tmdb_id}",
                        "proxyHeaders": {
                            "request": {
                                "Referer": f"{CNO}/",
                                "Origin": CNO,
                                "User-Agent": UA,
                            }
                        },
                    },
                }
            )

    return j({"streams": streams})


@app.get("/")
def root():
    return j({"ok": True, "version": ADDON_VERSION, "manifest": "/manifest.json"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=51823, threaded=True)

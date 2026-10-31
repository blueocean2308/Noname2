#!/usr/bin/env python3
"""Fprime Stremio/Nuvio addon — Storage + Astra (AES-GCM). Direct CDN, no VPS proxy."""

from __future__ import annotations

import re
import time
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

PORT = 51823
VERSION = "1.0.4"
ADDON_NAME = "Fprime"
ADDON_ID = "org.nuvio.fprime"

TMDB = "https://db.insidethisplaceiswarm.cc/3"
STORAGE = "https://storage.insidethisplaceiswarm.cc/content"
ASTRA = "https://serverone.insidethisplaceiswarm.cc/astra"
ASTRA_KEY = bytes.fromhex(
    "cc668a8304b57864709efdfc0051a619e146c0c8b486f190a06d1c1cf96af698"
)
IMG = "https://image.tmdb.org/t/p"

# Headers khớp Lemur (browser thật)
UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36"
)
REQ_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://fprime.at/",
    "Origin": "https://fprime.at",
    "Accept": "*/*",
    "Accept-Language": "vi-VN",
    "Accept-Encoding": "identity",
}

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://fprime.at/",
        "Origin": "https://fprime.at",
    }
)

CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 900


def cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    if "Content-Type" not in resp.headers:
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp


def cached(key: str, fn):
    now = time.time()
    if key in CACHE:
        ts, val = CACHE[key]
        if now - ts < CACHE_TTL:
            return val
    val = fn()
    CACHE[key] = (now, val)
    return val


def get_json(url: str) -> Optional[dict]:
    try:
        r = SESSION.get(url, timeout=12)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def decrypt_astra(enc: str) -> Optional[str]:
    try:
        if enc.startswith("as_"):
            enc = enc[3:]
        raw = bytes.fromhex(enc)
        return AESGCM(ASTRA_KEY).decrypt(raw[:12], raw[12:], None).decode()
    except Exception:
        return None


def tmdb_find_imdb(imdb: str) -> Optional[dict]:
    def _():
        data = get_json(f"{TMDB}/find/{imdb}?external_source=imdb_id")
        if not data:
            return None
        for key in ("movie_results", "tv_results"):
            arr = data.get(key) or []
            if arr:
                item = arr[0]
                item["_media"] = "movie" if key == "movie_results" else "tv"
                return item
        return None

    return cached(f"find:{imdb}", _)


def parse_id(id_str: str) -> Optional[dict]:
    id_str = id_str.replace(".json", "")

    # tmdb:123:1:2
    m = re.match(r"^tmdb[:\-]?(\d+):(\d+):(\d+)$", id_str)
    if m:
        return {
            "type": "tv",
            "tmdb": int(m.group(1)),
            "season": int(m.group(2)),
            "episode": int(m.group(3)),
        }

    # tt1234567:1:2
    m = re.match(r"^(tt\d+):(\d+):(\d+)$", id_str)
    if m:
        imdb, season, episode = m.group(1), int(m.group(2)), int(m.group(3))
        found = tmdb_find_imdb(imdb)
        if not found:
            return None
        return {
            "type": "tv",
            "tmdb": found["id"],
            "season": season,
            "episode": episode,
            "name": found.get("name") or found.get("title"),
        }

    if id_str.startswith("tt"):
        found = tmdb_find_imdb(id_str)
        if not found:
            return None
        return {
            "type": "movie" if found.get("_media") == "movie" else "tv",
            "tmdb": found["id"],
            "name": found.get("title") or found.get("name"),
        }

    m = re.match(r"^tmdb[:\-]?(\d+)$", id_str)
    if m:
        return {"type": "movie", "tmdb": int(m.group(1))}
    return None


def poster_url(path: Optional[str], size: str = "w500") -> Optional[str]:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"{IMG}/{size}{path}"


def meta_from_tmdb(mtype: str, tmdb_id: int) -> Optional[dict]:
    def _():
        path = "movie" if mtype == "movie" else "tv"
        data = get_json(f"{TMDB}/{path}/{tmdb_id}?language=en-US")
        if not data:
            return None
        name = data.get("title") or data.get("name") or ""
        year = (data.get("release_date") or data.get("first_air_date") or "")[:4]
        genres = [g["name"] for g in (data.get("genres") or [])]
        meta = {
            "id": f"tmdb:{tmdb_id}",
            "type": "movie" if mtype == "movie" else "series",
            "name": name,
            "poster": poster_url(data.get("poster_path")),
            "background": poster_url(data.get("backdrop_path"), "w1280"),
            "description": data.get("overview") or "",
            "releaseInfo": year,
            "imdbRating": str(data.get("vote_average") or ""),
            "genres": genres,
        }
        if mtype == "tv":
            videos = []
            for s in range(1, min((data.get("number_of_seasons") or 1), 30) + 1):
                season = get_json(f"{TMDB}/tv/{tmdb_id}/season/{s}?language=en-US")
                if not season:
                    continue
                for ep in season.get("episodes") or []:
                    videos.append(
                        {
                            "id": f"tmdb:{tmdb_id}:{s}:{ep.get('episode_number')}",
                            "title": ep.get("name") or f"Tập {ep.get('episode_number')}",
                            "season": s,
                            "episode": ep.get("episode_number"),
                            "released": ep.get("air_date") or "",
                            "thumbnail": poster_url(ep.get("still_path"), "w300"),
                        }
                    )
            meta["videos"] = videos
        return meta

    return cached(f"meta:{mtype}:{tmdb_id}", _)


def catalog_items(kind: str, mtype: str, skip: int = 0) -> list:
    def _():
        path = "movie" if mtype == "movie" else "tv"
        endpoint = "trending" if kind == "trending" else "popular"
        if endpoint == "trending":
            url = f"{TMDB}/trending/{path}/week?language=en-US"
        else:
            url = f"{TMDB}/{path}/popular?language=en-US&page=1"
        data = get_json(url)
        if not data:
            return []
        out = []
        for item in (data.get("results") or [])[skip : skip + 30]:
            tid = item.get("id")
            name = item.get("title") or item.get("name") or ""
            out.append(
                {
                    "id": f"tmdb:{tid}",
                    "type": "movie" if mtype == "movie" else "series",
                    "name": name,
                    "poster": poster_url(item.get("poster_path")),
                    "releaseInfo": (item.get("release_date") or item.get("first_air_date") or "")[:4],
                }
            )
        return out

    return cached(f"cat:{kind}:{mtype}:{skip}", _) or []


def get_storage_url(mtype: str, tmdb: int, season: int = 0, episode: int = 0) -> Optional[str]:
    if mtype == "movie":
        data = get_json(f"{STORAGE}/movie/{tmdb}")
    else:
        data = get_json(f"{STORAGE}/tv/{tmdb}/{season}/{episode}")
    if data and data.get("found") and data.get("url"):
        return data["url"]
    return None


def get_astra_url(mtype: str, tmdb: int, season: int = 0, episode: int = 0) -> Optional[str]:
    if mtype == "movie":
        url = f"{ASTRA}?tmdbId={tmdb}&type=movie"
    else:
        url = f"{ASTRA}?tmdbId={tmdb}&type=tv&season={season}&episode={episode}"
    data = get_json(url)
    if not data:
        return None
    enc = (data.get("sources") or {}).get("Astra", {}).get("url")
    if not enc:
        return None
    return decrypt_astra(enc)


def max_height_of_master(url: str) -> int:
    """Optional quality probe — does not replace returned URL."""
    try:
        r = SESSION.get(url, timeout=10, headers=REQ_HEADERS)
        if r.status_code != 200 or not (r.text or "").lstrip().startswith("#EXTM3U"):
            return 0
        h = 0
        for line in r.text.splitlines():
            m = re.search(r"RESOLUTION=\d+x(\d+)", line)
            if m:
                h = max(h, int(m.group(1)))
        return h
    except Exception:
        return 0


def quality_label(height: int) -> str:
    if height >= 2160:
        return "4K"
    if height >= 1440:
        return "1440p"
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    if height > 0:
        return f"{height}p"
    return "AUTO"


def build_streams(info: dict) -> list[dict]:
    mtype = info["type"]
    tmdb = info["tmdb"]
    season = info.get("season") or 0
    episode = info.get("episode") or 0
    streams: list[dict] = []
    seen = set()

    pairs = []
    storage = get_storage_url(mtype, tmdb, season, episode)
    if storage:
        pairs.append(("Storage", storage))
    astra = get_astra_url(mtype, tmdb, season, episode)
    if astra:
        pairs.append(("Astra", astra))

    for server, url in pairs:
        if not url or url in seen:
            continue
        seen.add(url)
        h = max_height_of_master(url)
        # Chỉ hiện nếu probe được >=1080 hoặc probe fail (vẫn trả, để tránh false-empty)
        if h and h < 1080:
            continue
        q = quality_label(h) if h else "1080p"
        if episode:
            title = f"Tập {episode}\n{q} · {server}"
        else:
            title = f"{q} · {server}"
        streams.append(
            {
                "name": f"Fprime {q}",
                "title": title,
                "url": url,
                "behaviorHints": {
                    "notWebReady": True,
                    "bingeGroup": f"fprime-{tmdb}-{season}-{episode}-{server}",
                    "proxyHeaders": {"request": dict(REQ_HEADERS)},
                },
            }
        )
    return streams


@app.route("/")
@app.route("/manifest.json")
def manifest():
    body = {
        "id": ADDON_ID,
        "version": VERSION,
        "name": ADDON_NAME,
        "description": "Fprime — movies & series (1080p+). Storage + Astra.",
        "logo": "https://fprime.at/icon-192.png",
        "resources": ["catalog", "meta", "stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "tmdb"],
        "catalogs": [
            {
                "type": "movie",
                "id": "fprime-trending-movie",
                "name": "Fprime Trending",
                "extra": [{"name": "skip"}],
            },
            {
                "type": "movie",
                "id": "fprime-popular-movie",
                "name": "Fprime Popular",
                "extra": [{"name": "skip"}],
            },
            {
                "type": "series",
                "id": "fprime-trending-series",
                "name": "Fprime Trending",
                "extra": [{"name": "skip"}],
            },
            {
                "type": "series",
                "id": "fprime-popular-series",
                "name": "Fprime Popular",
                "extra": [{"name": "skip"}],
            },
            {
                "type": "movie",
                "id": "fprime-search-movie",
                "name": "Fprime Search",
                "extra": [{"name": "search", "isRequired": True}],
            },
            {
                "type": "series",
                "id": "fprime-search-series",
                "name": "Fprime Search",
                "extra": [{"name": "search", "isRequired": True}],
            },
        ],
        "behaviorHints": {"configurable": False},
    }
    return cors(Response(jsonify(body).get_data(), mimetype="application/json"))


@app.route("/catalog/<type_>/<id_>.json")
def catalog(type_, id_):
    skip = 0
    search = None
    extra = request.args.get("search") or ""
    # path style extra
    if "search=" in id_:
        # rare
        pass
    q = request.args.get("search")
    skip_s = request.args.get("skip")
    if skip_s:
        try:
            skip = int(skip_s)
        except Exception:
            skip = 0

    mtype = "movie" if type_ == "movie" else "tv"
    if "search" in id_ or q:
        keyword = q or ""
        def _search():
            path = "movie" if mtype == "movie" else "tv"
            data = get_json(
                f"{TMDB}/search/{path}?query={requests.utils.quote(keyword)}&language=en-US"
            )
            out = []
            for item in (data or {}).get("results") or []:
                tid = item.get("id")
                out.append(
                    {
                        "id": f"tmdb:{tid}",
                        "type": "movie" if mtype == "movie" else "series",
                        "name": item.get("title") or item.get("name") or "",
                        "poster": poster_url(item.get("poster_path")),
                        "releaseInfo": (
                            item.get("release_date") or item.get("first_air_date") or ""
                        )[:4],
                    }
                )
            return out

        metas = cached(f"search:{mtype}:{keyword}", _search) if keyword else []
        return cors(Response(jsonify({"metas": metas or []}).get_data(), mimetype="application/json"))

    kind = "trending" if "trending" in id_ else "popular"
    metas = catalog_items(kind, mtype, skip)
    return cors(Response(jsonify({"metas": metas}).get_data(), mimetype="application/json"))


@app.route("/meta/<type_>/<path:id_>")
def meta(type_, id_):
    id_ = id_.replace(".json", "")
    info = parse_id(id_)
    if not info:
        # tmdb:123 series
        m = re.match(r"^tmdb[:\-]?(\d+)$", id_)
        if m:
            mtype = "movie" if type_ == "movie" else "tv"
            meta_obj = meta_from_tmdb(mtype, int(m.group(1)))
            return cors(
                Response(jsonify({"meta": meta_obj or {}}).get_data(), mimetype="application/json")
            )
        return cors(Response(jsonify({"meta": {}}).get_data(), mimetype="application/json"))

    mtype = "movie" if info["type"] == "movie" else "tv"
    meta_obj = meta_from_tmdb(mtype, info["tmdb"])
    if meta_obj and info.get("type") == "movie":
        meta_obj["id"] = id_ if id_.startswith("tt") else meta_obj["id"]
    return cors(Response(jsonify({"meta": meta_obj or {}}).get_data(), mimetype="application/json"))


@app.route("/stream/<type_>/<path:id_>")
def stream(type_, id_):
    id_ = id_.replace(".json", "")
    info = parse_id(id_)
    if not info:
        m = re.match(r"^tmdb[:\-]?(\d+)(?::(\d+):(\d+))?$", id_)
        if m:
            info = {
                "type": "movie" if type_ == "movie" else "tv",
                "tmdb": int(m.group(1)),
                "season": int(m.group(2) or 0),
                "episode": int(m.group(3) or 0),
            }
            if type_ == "series" or type_ == "tv":
                info["type"] = "tv"
        else:
            return cors(Response(jsonify({"streams": []}).get_data(), mimetype="application/json"))

    if type_ in ("series", "tv"):
        info["type"] = "tv"

    streams = build_streams(info)
    return cors(Response(jsonify({"streams": streams}).get_data(), mimetype="application/json"))


@app.route("/health")
def health():
    return cors(Response(jsonify({"ok": True, "version": VERSION}).get_data(), mimetype="application/json"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)

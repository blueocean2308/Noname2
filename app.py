#!/usr/bin/env python3
"""67Movies / Ravenflix addon for Nuvio - VPS + curl_cffi"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from flask import Flask, Response
from curl_cffi import requests as cffi_requests

app = Flask(__name__)

MANIFEST = {
    "id": "org.nuvio.ravenflix",
    "version": "1.1.0",
    "name": "67Movies",
    "description": "Ravenflix / Vidlove (MovieBox) · 1080p+",
    "resources": ["catalog", "meta", "stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt", "tmdb:", "rf:"],
    "catalogs": [
        {
            "type": "movie",
            "id": "rf_movie_popular",
            "name": "67Movies · Popular",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "movie",
            "id": "rf_movie_top",
            "name": "67Movies · Top Rated",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "rf_tv_popular",
            "name": "67Movies · TV Popular",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "rf_tv_top",
            "name": "67Movies · TV Top Rated",
            "extra": [{"name": "skip", "isRequired": False}],
        },
    ],
}

API = "https://api.ravenflix.st"
REFERER = "https://player.vidlove.st/"
ORIGIN = "https://player.vidlove.st"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TMDB_KEY = "a46c50a0ccb1bafe2b15665df7fad7e1"
TMDB = "https://api.themoviedb.org/3"
SOURCES = ["moviebox2", "moviebox", "megaknight", "dcloud", "tcloud", "vidapi"]
MIN_QUALITY = 1080
CACHE_TTL = 600
_cache: dict[str, tuple[float, Any]] = {}
_session = cffi_requests.Session()


def cors_json(data: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(data, ensure_ascii=False),
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
    if len(_cache) > 600:
        for k, _ in sorted(_cache.items(), key=lambda x: x[1][0])[:120]:
            _cache.pop(k, None)


def http_get_json(url: str, raven_headers: bool = False) -> Optional[Any]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    if raven_headers:
        headers["Origin"] = ORIGIN
        headers["Referer"] = REFERER
    try:
        r = _session.get(url, headers=headers, impersonate="chrome131", timeout=25)
        if r.status_code != 200:
            return None
        return r.json()
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
    if parts[0] in ("tmdb", "rf"):
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
    url = f"{TMDB}/find/{imdb}?api_key={TMDB_KEY}&external_source=imdb_id"
    data = http_get_json(url)
    result = None
    if data:
        if data.get("movie_results"):
            result = {"tmdb": str(data["movie_results"][0]["id"]), "type": "movie"}
        elif data.get("tv_results"):
            result = {"tmdb": str(data["tv_results"][0]["id"]), "type": "series"}
    cache_set(ck, result)
    return result


def poster_url(path: Optional[str], size: str = "w500") -> Optional[str]:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"https://image.tmdb.org/t/p/{size}{path}"


def tmdb_item_to_meta(item: dict, type_: str) -> dict:
    tmdb_id = item.get("id")
    title = item.get("title") or item.get("name") or "Unknown"
    year = (item.get("release_date") or item.get("first_air_date") or "")[:4]
    meta = {
        "id": f"tmdb:{tmdb_id}",
        "type": type_,
        "name": title,
        "poster": poster_url(item.get("poster_path")),
        "background": poster_url(item.get("backdrop_path"), "w1280"),
        "description": item.get("overview") or "",
        "releaseInfo": year,
    }
    if item.get("vote_average"):
        meta["imdbRating"] = str(round(float(item["vote_average"]), 1))
    return meta


def fetch_catalog(type_: str, catalog_id: str, skip: int = 0) -> list[dict]:
    page = max(1, skip // 20 + 1)
    if catalog_id.endswith("top"):
        path = f"/{type_}/top_rated"
    else:
        path = f"/{type_}/popular"
    url = f"{TMDB}{path}?api_key={TMDB_KEY}&language=en-US&page={page}"
    ck = f"cat:{catalog_id}:{page}"
    hit = cache_get(ck)
    if hit is not None:
        return hit
    data = http_get_json(url) or {}
    results = data.get("results") or []
    metas = [tmdb_item_to_meta(x, type_) for x in results]
    cache_set(ck, metas)
    return metas


def fetch_meta(type_: str, tmdb_id: str) -> Optional[dict]:
    ck = f"meta:{type_}:{tmdb_id}"
    hit = cache_get(ck)
    if hit is not None:
        return hit
    url = f"{TMDB}/{type_}/{tmdb_id}?api_key={TMDB_KEY}&language=en-US"
    data = http_get_json(url)
    if not data:
        return None
    stype = "series" if type_ == "tv" else "movie"
    meta = tmdb_item_to_meta(data, stype)
    genres = [g.get("name") for g in (data.get("genres") or []) if g.get("name")]
    if genres:
        meta["genres"] = genres
    runtime = data.get("runtime")
    if runtime:
        meta["runtime"] = f"{runtime} min"
    if type_ == "tv":
        videos = []
        for s in data.get("seasons") or []:
            sn = s.get("season_number") or 0
            if sn < 1:
                continue
            ep_count = s.get("episode_count") or 0
            for ep in range(1, ep_count + 1):
                videos.append(
                    {
                        "id": f"tmdb:{tmdb_id}:{sn}:{ep}",
                        "title": f"Tập {ep}",
                        "season": sn,
                        "episode": ep,
                    }
                )
        meta["videos"] = videos
    cache_set(ck, meta)
    return meta


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
    data = http_get_json(API + path, raven_headers=True)
    src = None
    if isinstance(data, dict) and data.get("source"):
        src = data["source"]
    cache_set(ck, src)
    return src


def quality_num(text: str) -> int:
    t = (text or "").lower()
    if "2160" in t or "4k" in t or "uhd" in t:
        return 2160
    if "1440" in t or "2k" in t:
        return 1440
    m = re.search(r"(\d{3,4})\s*p", t)
    if m:
        return int(m.group(1))
    return 0


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
            qlabel = str(q.get("quality") or "")
            qn = quality_num(qlabel)
            if qn < MIN_QUALITY:
                continue
            codec = q.get("codec") or ""
            qtype = q.get("type") or "mp4"
            quality = qlabel.upper() if qlabel else f"{qn}P"
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

    if not streams and src.get("url"):
        blob = f"{src.get('label','')} {src.get('source','')}"
        qn = quality_num(blob)
        is_hls = isinstance(src.get("manifest"), str) and "#EXTM3U" in src["manifest"]
        if is_hls or qn >= MIN_QUALITY:
            extra = " · HLS" if is_hls else ""
            qtxt = f"{qn}p" if qn else "FHD"
            streams.append(
                {
                    "name": "67Movies",
                    "title": f"{label} · {qtxt}{extra}\n{src.get('source') or 'stream'}",
                    "url": src["url"],
                    "behaviorHints": proxy,
                }
            )

    streams.sort(key=lambda s: quality_num(s.get("title") or ""), reverse=True)
    return streams


@app.route("/manifest.json")
@app.route("/")
def manifest():
    return cors_json(MANIFEST)


@app.route("/catalog/<type_>/<catalog_id>.json")
@app.route("/catalog/<type_>/<catalog_id>/<path:extra>.json")
def catalog(type_: str, catalog_id: str, extra: str = ""):
    skip = 0
    if extra:
        m = re.search(r"skip=(\d+)", extra)
        if m:
            skip = int(m.group(1))
    tmdb_type = "tv" if type_ == "series" else "movie"
    metas = fetch_catalog(tmdb_type, catalog_id, skip)
    return cors_json({"metas": metas})


@app.route("/meta/<type_>/<path:id_>")
def meta(type_: str, id_: str):
    if id_.endswith(".json"):
        id_ = id_[:-5]
    parsed = parse_id(id_)
    tmdb = parsed["tmdb"]
    if not tmdb and parsed["imdb"]:
        mapped = imdb_to_tmdb(parsed["imdb"])
        if mapped:
            tmdb = mapped["tmdb"]
            if mapped["type"] == "series":
                type_ = "series"
    if not tmdb:
        return cors_json({"meta": {}})
    tmdb_type = "tv" if type_ == "series" else "movie"
    m = fetch_meta(tmdb_type, tmdb)
    return cors_json({"meta": m or {}})


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

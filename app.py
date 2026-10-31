#!/usr/bin/env python3
"""Fprime Stremio/Nuvio addon — catalog + streams (Storage + Astra AES-GCM)."""

from __future__ import annotations

import re
import time
from typing import Any, Optional
from urllib.parse import quote

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

PORT = 51823
VERSION = "1.0.0"
ADDON_NAME = "Fprime"
ADDON_ID = "org.nuvio.fprime"

TMDB = "https://db.insidethisplaceiswarm.cc/3"
STORAGE = "https://storage.insidethisplaceiswarm.cc/content"
ASTRA = "https://serverone.insidethisplaceiswarm.cc/astra"
ASTRA_KEY = bytes.fromhex(
    "cc668a8304b57864709efdfc0051a619e146c0c8b486f190a06d1c1cf96af698"
)
IMG = "https://image.tmdb.org/t/p"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
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
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp


def cached(key: str, fn, ttl: int = CACHE_TTL):
    now = time.time()
    hit = CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    CACHE[key] = (now, val)
    return val


def get_json(url: str, timeout: int = 15) -> Optional[dict]:
    try:
        r = SESSION.get(url, timeout=timeout)
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
        if len(raw) < 28:
            return None
        iv, ct = raw[:12], raw[12:]
        plain = AESGCM(ASTRA_KEY).decrypt(iv, ct, None)
        return plain.decode("utf-8", errors="ignore")
    except Exception:
        return None


def imdb_to_tmdb(imdb_id: str) -> Optional[tuple[str, int]]:
    def _fetch():
        data = get_json(
            f"{TMDB}/find/{imdb_id}?external_source=imdb_id&language=en-US"
        )
        if not data:
            return None
        for key, mtype in (("movie_results", "movie"), ("tv_results", "tv")):
            arr = data.get(key) or []
            if arr:
                return mtype, int(arr[0]["id"])
        return None

    return cached(f"imdb:{imdb_id}", _fetch, 3600)


def parse_stream_id(sid: str) -> Optional[dict]:
    sid = sid.replace(".json", "").strip()
    if sid.startswith("tmdb:"):
        parts = sid.split(":")
        if len(parts) == 3 and parts[1] in ("movie", "tv"):
            return {"type": parts[1], "tmdb": int(parts[2])}
        if len(parts) == 5 and parts[1] == "tv":
            return {
                "type": "tv",
                "tmdb": int(parts[2]),
                "season": int(parts[3]),
                "episode": int(parts[4]),
            }
        return None

    m = re.match(r"^(tt\d+)(?::(\d+):(\d+))?$", sid)
    if not m:
        return None
    imdb = m.group(1)
    mapped = imdb_to_tmdb(imdb)
    if not mapped:
        return None
    mtype, tid = mapped
    out = {"type": mtype, "tmdb": tid, "imdb": imdb}
    if m.group(2) and m.group(3):
        out["season"] = int(m.group(2))
        out["episode"] = int(m.group(3))
        out["type"] = "tv"
    return out


def poster_url(path: Optional[str], size: str = "w500") -> Optional[str]:
    if not path:
        return None
    return f"{IMG}/{size}{path}"


def meta_from_tmdb(item: dict, media_type: str) -> dict:
    tid = item.get("id")
    title = item.get("title") or item.get("name") or "Unknown"
    year = (item.get("release_date") or item.get("first_air_date") or "")[:4]
    ext = item.get("external_ids") or {}
    imdb = ext.get("imdb_id")
    mid = imdb or f"tmdb:{media_type}:{tid}"
    meta = {
        "id": mid,
        "type": "movie" if media_type == "movie" else "series",
        "name": title,
        "poster": poster_url(item.get("poster_path")),
        "background": poster_url(item.get("backdrop_path"), "w1280"),
        "description": item.get("overview") or "",
        "releaseInfo": year,
        "imdbRating": str(round(item.get("vote_average") or 0, 1))
        if item.get("vote_average")
        else None,
        "genres": [g["name"] for g in (item.get("genres") or []) if g.get("name")],
    }
    return {k: v for k, v in meta.items() if v is not None}


def catalog_item(item: dict) -> Optional[dict]:
    mtype = item.get("media_type") or ("movie" if "title" in item else "tv")
    if mtype not in ("movie", "tv"):
        return None
    title = item.get("title") or item.get("name")
    if not title:
        return None
    year = (item.get("release_date") or item.get("first_air_date") or "")[:4]
    return {
        "id": f"tmdb:{mtype}:{item['id']}",
        "type": "movie" if mtype == "movie" else "series",
        "name": title,
        "poster": poster_url(item.get("poster_path")),
        "background": poster_url(item.get("backdrop_path"), "w1280"),
        "description": item.get("overview") or "",
        "releaseInfo": year,
    }


def fetch_master_variants(url: str) -> list[dict]:
    try:
        r = SESSION.get(
            url,
            timeout=12,
            headers={"Referer": "https://fprime.at/", "User-Agent": UA},
        )
        if r.status_code != 200:
            return [{"url": url, "quality": "AUTO", "height": 0}]
        text = r.text
        if "#EXT-X-STREAM-INF" not in text:
            return [{"url": url, "quality": "AUTO", "height": 0}]
        lines = text.splitlines()
        variants = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXT-X-STREAM-INF"):
                height = 0
                m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                if m:
                    height = int(m.group(2))
                bw = 0
                mb = re.search(r"BANDWIDTH=(\d+)", line)
                if mb:
                    bw = int(mb.group(1))
                if i + 1 < len(lines):
                    stream_url = lines[i + 1].strip()
                    if stream_url and not stream_url.startswith("#"):
                        if stream_url.startswith("http"):
                            full = stream_url
                        else:
                            base = url.rsplit("/", 1)[0]
                            full = base + "/" + stream_url.lstrip("/")
                        variants.append(
                            {"url": full, "height": height, "bandwidth": bw}
                        )
                i += 2
            else:
                i += 1
        good = [v for v in variants if v["height"] >= 1080]
        if not good:
            if variants:
                variants.sort(
                    key=lambda x: (x["height"], x["bandwidth"]), reverse=True
                )
                top = variants[0]
                q = f"{top['height']}p" if top["height"] else "AUTO"
                return [{"url": top["url"], "quality": q, "height": top["height"]}]
            return [{"url": url, "quality": "AUTO", "height": 0}]
        good.sort(key=lambda x: (x["height"], x["bandwidth"]), reverse=True)
        out = []
        for v in good:
            if v["height"] >= 2160:
                q = "4K"
            elif v["height"] >= 1440:
                q = "1440p"
            else:
                q = "1080p"
            out.append({"url": v["url"], "quality": q, "height": v["height"]})
        return out
    except Exception:
        return [{"url": url, "quality": "AUTO", "height": 0}]


def get_storage_url(
    mtype: str, tmdb: int, season: int = 0, episode: int = 0
) -> Optional[str]:
    if mtype == "movie":
        data = get_json(f"{STORAGE}/movie/{tmdb}")
    else:
        data = get_json(f"{STORAGE}/tv/{tmdb}/{season}/{episode}")
    if data and data.get("found") and data.get("url"):
        return data["url"]
    return None


def get_astra_url(
    mtype: str, tmdb: int, season: int = 0, episode: int = 0
) -> Optional[str]:
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


def build_streams(info: dict) -> list[dict]:
    mtype = info["type"]
    tmdb = info["tmdb"]
    season = info.get("season") or 0
    episode = info.get("episode") or 0
    streams: list[dict] = []
    seen = set()

    sources = []
    storage = get_storage_url(mtype, tmdb, season, episode)
    if storage:
        sources.append(("Storage", storage))
    astra = get_astra_url(mtype, tmdb, season, episode)
    if astra:
        sources.append(("Astra", astra))

    for server, raw_url in sources:
        variants = fetch_master_variants(raw_url)
        for v in variants:
            if v["height"] and v["height"] < 1080:
                continue
            u = v["url"]
            if u in seen:
                continue
            seen.add(u)
            q = v["quality"]
            if episode:
                title = f"Tập {episode}\n{q} · {server}"
            else:
                title = f"{q} · {server}"
            streams.append(
                {
                    "name": f"Fprime {q}",
                    "title": title,
                    "url": u,
                    "behaviorHints": {
                        "notWebReady": False,
                        "bingeGroup": f"fprime-{tmdb}-{season}-{episode}-{server}",
                        "proxyHeaders": {
                            "request": {
                                "Referer": "https://fprime.at/",
                                "Origin": "https://fprime.at/",
                                "User-Agent": UA,
                            }
                        },
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
                "id": "fprime-search",
                "name": "Fprime Search",
                "extra": [{"name": "search", "isRequired": True}],
            },
            {
                "type": "series",
                "id": "fprime-search",
                "name": "Fprime Search",
                "extra": [{"name": "search", "isRequired": True}],
            },
        ],
        "behaviorHints": {"configurable": False, "configurationRequired": False},
    }
    return cors(jsonify(body))


@app.route("/catalog/<stype>/<cid>.json")
@app.route("/catalog/<stype>/<cid>/<path:extra>.json")
def catalog(stype: str, cid: str, extra: str = ""):
    search = None
    skip = 0
    if extra:
        for part in extra.replace(".json", "").split("&"):
            if part.startswith("search="):
                search = part.split("=", 1)[1]
            if part.startswith("skip="):
                try:
                    skip = int(part.split("=", 1)[1])
                except ValueError:
                    skip = 0
    if request.args.get("search"):
        search = request.args.get("search")
    if request.args.get("skip"):
        try:
            skip = int(request.args.get("skip"))
        except ValueError:
            pass

    page = max(1, skip // 20 + 1)

    def load():
        items = []
        if search:
            data = get_json(
                f"{TMDB}/search/multi?query={quote(search)}"
                f"&language=en-US&page={page}&include_adult=false"
            )
            for it in (data or {}).get("results") or []:
                mt = it.get("media_type")
                if stype == "movie" and mt != "movie":
                    continue
                if stype == "series" and mt != "tv":
                    continue
                c = catalog_item(it)
                if c:
                    items.append(c)
            return items

        if "trending" in cid:
            path = "movie" if stype == "movie" else "tv"
            data = get_json(
                f"{TMDB}/trending/{path}/week?language=en-US&page={page}"
            )
        else:
            path = "movie" if stype == "movie" else "tv"
            data = get_json(
                f"{TMDB}/discover/{path}?language=en-US&sort_by=popularity.desc"
                f"&vote_count.gte=50&page={page}&include_adult=false"
            )
        for it in (data or {}).get("results") or []:
            it.setdefault("media_type", "movie" if stype == "movie" else "tv")
            c = catalog_item(it)
            if c:
                items.append(c)
        return items

    metas = cached(f"cat:{stype}:{cid}:{search}:{page}", load, 600)
    return cors(jsonify({"metas": metas or []}))


@app.route("/meta/<stype>/<path:sid>")
def meta(stype: str, sid: str):
    sid = sid.replace(".json", "")
    info = parse_stream_id(sid)
    if not info:
        return cors(jsonify({"meta": {}}))

    mtype = info["type"]
    tid = info["tmdb"]

    def load():
        if mtype == "movie":
            data = get_json(
                f"{TMDB}/movie/{tid}?language=en-US&append_to_response=external_ids"
            )
            if not data:
                return {}
            return meta_from_tmdb(data, "movie")
        data = get_json(
            f"{TMDB}/tv/{tid}?language=en-US&append_to_response=external_ids"
        )
        if not data:
            return {}
        base = meta_from_tmdb(data, "tv")
        videos = []
        imdb = base.get("id") if str(base.get("id", "")).startswith("tt") else None
        for season in data.get("seasons") or []:
            sn = season.get("season_number") or 0
            if sn < 1:
                continue
            sdata = get_json(f"{TMDB}/tv/{tid}/season/{sn}?language=en-US")
            for ep in (sdata or {}).get("episodes") or []:
                en = ep.get("episode_number") or 0
                eid = (
                    f"{imdb}:{sn}:{en}" if imdb else f"tmdb:tv:{tid}:{sn}:{en}"
                )
                videos.append(
                    {
                        "id": eid,
                        "title": f"Tập {en}"
                        + (f" — {ep['name']}" if ep.get("name") else ""),
                        "season": sn,
                        "episode": en,
                        "released": ep.get("air_date") or "",
                        "thumbnail": poster_url(ep.get("still_path"), "w300"),
                    }
                )
        base["videos"] = videos
        return base

    meta_obj = cached(f"meta:{mtype}:{tid}", load, 1800)
    return cors(jsonify({"meta": meta_obj or {}}))


@app.route("/stream/<stype>/<path:sid>")
def stream(stype: str, sid: str):
    sid = sid.replace(".json", "")
    info = parse_stream_id(sid)
    if not info:
        return cors(jsonify({"streams": []}))
    if info["type"] == "tv" and not info.get("episode"):
        return cors(jsonify({"streams": []}))

    def load():
        return build_streams(info)

    key = (
        f"stream:{info['type']}:{info['tmdb']}:"
        f"{info.get('season', 0)}:{info.get('episode', 0)}"
    )
    streams = cached(key, load, 300)
    return cors(jsonify({"streams": streams or []}))


@app.route("/<path:_any>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def options(_any: str = ""):
    return cors(Response(status=204))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)

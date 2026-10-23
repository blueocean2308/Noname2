#!/usr/bin/env python3
"""CNO (cno.to) VPS addon 1.0.3
- Manifest name CNO
- Catalog from /api/browse
- Stream: scan servers 1-40, movie + tv paths
- Quality label by width/height (4K ultrawide)
- Episode title: Tập N
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
ADDON_VERSION = "1.0.4"
SERVER_IDS = list(range(1, 41))

API_CACHE: dict = {}
TMDB_TTL = 86400
API_TTL = 180


def S():
    s = cfreq.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def j(data, code=200):
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=code,
        mimetype="application/json",
        headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"},
    )


def public_root():
    host = request.headers.get("Host") or PUBLIC_HOST
    if host.startswith("127.") or "localhost" in host:
        host = PUBLIC_HOST
    return f"http://{host}"


def cache_get(key):
    item = API_CACHE.get(key)
    if not item:
        return None
    if item[1] <= time.time():
        API_CACHE.pop(key, None)
        return None
    return item[0]


def cache_put(key, data, ttl):
    if len(API_CACHE) > 500:
        now = time.time()
        for k in [k for k, v in list(API_CACHE.items()) if v[1] <= now][:100]:
            API_CACHE.pop(k, None)
    API_CACHE[key] = (data, time.time() + ttl)


def cno_get(path: str, referer: str | None = None, prefer_text: bool = False):
    url = path if path.startswith("http") else f"{CNO}{path}"
    r = S().get(
        url,
        headers={
            "Referer": referer or f"{CNO}/",
            "Origin": CNO,
            "Accept": "application/json, application/vnd.apple.mpegurl, */*",
        },
        impersonate=IMP,
        timeout=18,
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
            params={"api_key": TMDB_KEY, "external_source": "imdb_id", "language": "en-US"},
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


def quality_label(w: int, h: int) -> str:
    if w >= 3840 or h >= 2160:
        return "4K"
    if h >= 1440 or w >= 2560:
        return "1440p"
    if h >= 1080 or w >= 1920:
        return "1080p"
    if h >= 720 or w >= 1280:
        return "720p"
    if h >= 480:
        return "480p"
    return f"{h}p" if h else "HD"


def parse_master_m3u8(text: str, base: str):
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            bw_m = re.search(r"BANDWIDTH=(\d+)", line)
            w = int(res_m.group(1)) if res_m else 0
            h = int(res_m.group(2)) if res_m else 0
            bw = int(bw_m.group(1)) if bw_m else 0
            if i + 1 < len(lines):
                u = lines[i + 1].strip()
                if u and not u.startswith("#"):
                    out.append({"width": w, "height": h, "bandwidth": bw, "url": urljoin(base, u)})
            i += 2
            continue
        i += 1
    out.sort(key=lambda x: (x["height"], x["width"], x["bandwidth"]), reverse=True)
    return out


def try_one_stream(path: str, referer: str):
    try:
        code, body = cno_get(path, referer=referer, prefer_text=True)
        if code != 200 or not isinstance(body, str):
            return None
        if not body.lstrip().startswith("#EXTM3U"):
            return None
        variants = parse_master_m3u8(body, CNO + path)
        # always keep master URL for full audio/sub tracks
        return {"master": CNO + path, "body": body, "variants": variants or []}
    except Exception:
        return None


def fetch_all_streams(kind: str, tmdb_id: int, season: int | None = None, episode: int | None = None):
    """Return list of {server, variants} for every working server."""
    paths = []
    if kind == "movie":
        ref = f"{CNO}/movie/{tmdb_id}"
        for sid in SERVER_IDS:
            paths.append((sid, f"/api/s/{sid}/movie/{tmdb_id}?v=3", ref))
    else:
        s, e = season or 1, episode or 1
        ref = f"{CNO}/watch/show/{tmdb_id}/{s}/{e}"
        for sid in SERVER_IDS:
            paths.append((sid, f"/api/s/{sid}/tv/{tmdb_id}/{s}/{e}?v=3", ref))
            paths.append((sid, f"/api/s/{sid}/show/{tmdb_id}/{s}/{e}?v=3", ref))

    found = []
    seen_sid = set()

    def work(item):
        sid, path, ref = item
        data = try_one_stream(path, ref)
        return sid, path, data

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(work, p) for p in paths]
        for fut in as_completed(futs):
            try:
                sid, path, data = fut.result()
            except Exception:
                continue
            if not data or sid in seen_sid:
                continue
            seen_sid.add(sid)
            found.append({
                "server": sid,
                "path": path,
                "master": data.get("master"),
                "variants": data.get("variants") or [],
            })

    found.sort(key=lambda x: x["server"])
    return found


def item_to_meta(item: dict):
    t = item.get("type") or "movie"
    tid = item.get("id")
    name = item.get("title") or ""
    year = str(item.get("year") or "")[:4]
    poster = item.get("poster") or ""
    if poster and poster.startswith("/"):
        poster = f"https://image.tmdb.org/t/p/w500{poster}"
    imdb = item.get("imdbId") or ""
    meta_id = imdb if imdb else f"cno:{t}:{tid}"
    return {
        "id": meta_id,
        "type": "series" if t == "tv" else "movie",
        "name": name,
        "poster": poster,
        "releaseInfo": year,
        "description": (item.get("overview") or "")[:300],
    }


@app.get("/manifest.json")
def manifest():
    root = public_root()
    return j(
        {
            "id": "org.nuvio.cno.vps",
            "version": ADDON_VERSION,
            "name": "CNO",
            "description": "CNO (cno.to) streams by TMDB id — multi-server",
            "resources": ["catalog", "meta", "stream"],
            "types": ["movie", "series"],
            "idPrefixes": ["tt", "cno:"],
            "catalogs": [
                {
                    "type": "movie",
                    "id": "cno-movies",
                    "name": "CNO Movies",
                    "extra": [{"name": "skip"}, {"name": "search", "isRequired": False}],
                },
                {
                    "type": "series",
                    "id": "cno-series",
                    "name": "CNO Series",
                    "extra": [{"name": "skip"}, {"name": "search", "isRequired": False}],
                },
            ],
            "logo": f"{root}/logo.png",
        }
    )


@app.get("/catalog/<ctype>/<cid>.json")
@app.get("/catalog/<ctype>/<cid>/<path:extra>.json")
def catalog(ctype, cid, extra=""):
    search = None
    skip = 0
    if extra:
        for part in extra.replace(".json", "").split("&"):
            if part.startswith("search="):
                search = part.split("=", 1)[1]
            if part.startswith("skip="):
                try:
                    skip = int(part.split("=", 1)[1])
                except Exception:
                    skip = 0

    page = max(1, skip // 20 + 1)
    api_type = "tv" if ctype == "series" else "movie"

    if search:
        # cno has no public search doc — use browse + client filter fallback via TMDB search name
        code, data = cno_get(f"/api/browse?type={api_type}&page={page}")
        results = data.get("results") if isinstance(data, dict) else []
        q = search.lower()
        results = [x for x in (results or []) if q in (x.get("title") or "").lower()]
    else:
        code, data = cno_get(f"/api/browse?type={api_type}&page={page}")
        results = data.get("results") if isinstance(data, dict) and code == 200 else []

    metas = [item_to_meta(x) for x in (results or [])[:40]]
    return j({"metas": metas})


@app.get("/meta/<mtype>/<mid>")
def meta(mtype, mid):
    mid = mid.replace(".json", "")
    if mid.startswith("tt"):
        kind, tmdb_id, title, year = tmdb_from_imdb(mid)
        if not tmdb_id:
            return j({"meta": {}})
        api_kind = "tv" if kind == "tv" else "movie"
        code, data = cno_get(f"/api/{api_kind}/{tmdb_id}")
        if code != 200 or not isinstance(data, dict):
            return j(
                {
                    "meta": {
                        "id": mid,
                        "type": "series" if kind == "tv" else "movie",
                        "name": title,
                        "releaseInfo": year,
                    }
                }
            )
        data["imdbId"] = mid
        meta_obj = item_to_meta(data)
        meta_obj["id"] = mid
        if kind == "tv":
            code2, season = cno_get(f"/api/tv/{tmdb_id}/season/1")
            videos = []
            if isinstance(season, dict):
                for ep in season.get("episodes") or []:
                    se, ee = ep.get("season") or 1, ep.get("episode") or 1
                    videos.append(
                        {
                            "id": f"{mid}:{se}:{ee}",
                            "title": ep.get("title") or f"Tập {ee}",
                            "season": se,
                            "episode": ee,
                            "released": ep.get("date") or "",
                        }
                    )
            # try more seasons if seasons count > 1
            seasons_n = int(data.get("seasons") or 1)
            for sn in range(2, min(seasons_n, 8) + 1):
                code3, sdata = cno_get(f"/api/tv/{tmdb_id}/season/{sn}")
                if not isinstance(sdata, dict):
                    continue
                for ep in sdata.get("episodes") or []:
                    se, ee = ep.get("season") or sn, ep.get("episode") or 1
                    videos.append(
                        {
                            "id": f"{mid}:{se}:{ee}",
                            "title": ep.get("title") or f"Tập {ee}",
                            "season": se,
                            "episode": ee,
                            "released": ep.get("date") or "",
                        }
                    )
            meta_obj["videos"] = videos
        return j({"meta": meta_obj})
    return j({"meta": {}})


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
    if stype == "movie":
        kind = "movie"
    elif stype == "series":
        kind = "tv"

    cache_key = f"st:{kind}:{tmdb_id}:{season}:{episode}"
    cached = cache_get(cache_key)
    if cached is not None:
        groups = cached
    else:
        groups = fetch_all_streams(kind, tmdb_id, season, episode)
        cache_put(cache_key, groups, API_TTL)

    streams = []
    name_base = title or imdb
    ybit = f" - {year}" if year else ""
    epbit = f" - Tập {int(episode)}" if episode is not None else ""

    for g in groups:
        variants = g.get("variants") or []
        srv = g["server"]
        master = g.get("master") or (CNO + g["path"])
        # quality from best variant if any
        fhd = [v for v in variants if v.get("height", 0) >= 1080 or v.get("width", 0) >= 1920]
        pick = (fhd or variants or [None])[0]
        if pick:
            q = quality_label(pick.get("width", 0), pick.get("height", 0))
        else:
            q = "HD"
        # Master only — direct variant often has no audio track for ExoPlayer
        streams.append(
            {
                "name": f"CNO {q} · S{srv}",
                "title": f"{name_base}{ybit}{epbit}\n{q} · Server {srv}",
                "url": master,
                "behaviorHints": {
                    "notWebReady": True,
                    "bingeGroup": f"cno-{tmdb_id}-s{srv}",
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
    return j({"ok": True, "version": ADDON_VERSION, "name": "CNO"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=51823, threaded=True)

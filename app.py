#!/usr/bin/env python3
"""Onflix VPS addon 1.1.1 — exact slug, filtered search, NC + m3u8."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urljoin, urlparse, unquote

from flask import Flask, Response, request
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from curl_cffi import requests as cfreq

app = Flask(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
IMP = "chrome120"
API = "https://k8s.onflixcdn.com/api"
SITE = "https://onflix.lat"
PUBLIC_HOST = "168.138.176.147:51823"
TMDB_KEY = "1adf1a2b5aece0ac5106302d3299f56f"
CACHE: dict = {}


def S():
    s = cfreq.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "vi,en;q=0.9",
            "Referer": SITE + "/",
        }
    )
    return s


def j(data, code=200):
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=code,
        mimetype="application/json; charset=utf-8",
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


def decrypt_bootstrap(envelope, api_url):
    if envelope.get("format") != "aesgcm-v1":
        return envelope
    iv = bytes.fromhex(envelope["iv"])
    data = base64.b64decode(
        envelope["data"] + "=" * ((4 - len(envelope["data"]) % 4) % 4)
    )
    aad = f"stream-bootstrap-v1\n{api_url}".encode()
    key = hashlib.sha256(aad).digest()
    return json.loads(AESGCM(key).decrypt(iv, data, aad).decode())


def playlist_key(vh):
    return hmac.new(b"stream-derive-v1", vh.encode(), hashlib.sha256).digest()


def decrypt_playlist(text, vh):
    if "#ENC-AESGCM" not in text:
        if text.lstrip().startswith("#EXTM3U"):
            return text
        raise ValueError("not m3u8")
    lines = text.strip().splitlines()
    m = re.match(r"^#ENC-AESGCM;iv=([a-fA-F0-9]{24})$", lines[1].strip())
    if not m:
        raise ValueError("bad envelope")
    iv = bytes.fromhex(m.group(1))
    combined = base64.b64decode(lines[3].strip())
    return AESGCM(playlist_key(vh)).decrypt(iv, combined, None).decode()


def streamc_m3u8(embed_url: str) -> str:
    host = urlparse(embed_url).hostname or "embed11.streamc.xyz"
    origin = f"https://{host}"
    s = S()
    body = {
        "action": "bootstrap",
        "request_grant": True,
        "playlist_format": "aesgcm-v2",
        "bootstrap_format": "aesgcm-v1",
        "pretty_url": True,
        "path_chunks": True,
        "referrer": "",
        "frame_origins": [],
    }
    r = s.post(
        embed_url,
        json=body,
        headers={
            "Content-Type": "application/json",
            "Referer": embed_url,
            "Origin": origin,
        },
        impersonate=IMP,
        timeout=25,
    )
    if r.status_code != 200:
        raise RuntimeError(f"bootstrap {r.status_code}")
    plain = decrypt_bootstrap(r.json(), embed_url)
    purl = (plain.get("preissued") or {}).get("playlist")
    if not purl:
        raise RuntimeError("no playlist")
    r2 = s.get(
        purl,
        headers={"Referer": embed_url, "Origin": origin},
        impersonate=IMP,
        timeout=25,
    )
    if r2.status_code != 200:
        raise RuntimeError(f"playlist {r2.status_code}")
    m3u8 = decrypt_playlist(r2.text, plain["video"])
    out = []
    for line in m3u8.splitlines():
        t = line.strip()
        out.append(urljoin(purl, t) if t and not t.startswith("#") else line)
    return "\n".join(out) + "\n"


def api_movies(page=1, type_=None, q=None):
    params = {"page": page}
    if type_:
        params["type"] = type_
    if q:
        params["q"] = q
    try:
        r = S().get(f"{API}/movies", params=params, impersonate=IMP, timeout=15)
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("data") or []
    except Exception:
        return []


def filter_search(items, q: str):
    """Keep items that match any meaningful token; if none match, return []."""
    if not q or not items:
        return items or []
    q = q.strip().lower()
    tokens = [t for t in re.split(r"[\s/:\-]+", q) if len(t) >= 2]
    # also full query without spaces
    compact = re.sub(r"\s+", "", q)
    out = []
    for it in items:
        title = str(it.get("title") or "").lower()
        origin = str(it.get("original_title") or "").lower()
        slug = str(it.get("slug") or "").lower()
        blob = f"{title} {origin} {slug} {title.replace(' ', '')} {origin.replace(' ', '')}"
        score = 0
        if q in title or q in origin or q in slug:
            score += 10
        if compact and compact in blob.replace(" ", ""):
            score += 5
        for tok in tokens:
            if tok in blob:
                score += 2
        if score > 0:
            out.append((score, it))
    out.sort(key=lambda x: -x[0])
    return [it for _, it in out]


def find_by_slug(slug: str):
    """Exact slug only — never return a random first hit."""
    # try q=slug and q=slug with spaces
    for q in (slug, slug.replace("-", " ")):
        for it in api_movies(q=q):
            if it.get("slug") == slug:
                return it
    # scan first pages of lists
    for type_ in (None, "phim-bo", "phim-le"):
        for page in range(1, 4):
            for it in api_movies(page=page, type_=type_):
                if it.get("slug") == slug:
                    return it
    return None


def item_meta(it):
    slug = str(it.get("slug") or "")
    stype = "series" if it.get("type") == "phim-bo" else "movie"
    return {
        "id": f"onflix:{slug}",
        "type": stype,
        "name": it.get("title") or it.get("name") or slug,
        "poster": it.get("poster_url") or it.get("thumb_url") or "",
        "posterShape": "poster",
        "background": it.get("poster_url") or "",
        "description": it.get("categories") or "",
        "releaseInfo": str(it.get("year") or ""),
        "genres": [
            x.strip()
            for x in str(it.get("categories") or "").split(",")
            if x.strip()
        ],
    }


def parse_episodes(slug: str):
    try:
        r = S().get(f"{SITE}/phim/{slug}", impersonate=IMP, timeout=20)
        if r.status_code != 200:
            return []
        html = r.text
    except Exception:
        return []

    episodes = []

    for m in re.finditer(
        r"(embed\d*)\.streamc\.xyz(?:\\+/|/)embed\.php\?hash=([a-f0-9]{16,})",
        html,
    ):
        emb = f"https://{m.group(1)}.streamc.xyz/embed.php?hash={m.group(2)}"
        episodes.append(
            {
                "kind": "streamc",
                "link_embed": emb,
                "name": str(len(episodes) + 1),
                "server_name": "Vietsub #1 (NC)",
            }
        )

    # m3u8 direct — many escape styles
    for m in re.finditer(
        r"https:(?:\\+/)+[a-z0-9.-]+(?:\\+/)+[^\s\"'\\]+?\.m3u8[^\s\"'\\]*",
        html,
        re.I,
    ):
        url = m.group(0).replace("\\/", "/").replace("\\/", "/")
        url = url.replace("\\", "")
        if "streamc" in url or "onflixstream" in url:
            continue
        if not url.startswith("http"):
            continue
        episodes.append(
            {
                "kind": "m3u8",
                "link_m3u8": url,
                "name": str(len(episodes) + 1),
                "server_name": "Vietsub",
            }
        )

    seen, out = set(), []
    for ep in episodes:
        key = ep.get("link_embed") or ep.get("link_m3u8")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(ep)
    return out


def tmdb_names(imdb_id: str):
    try:
        r = S().get(
            f"https://api.themoviedb.org/3/find/{imdb_id}",
            params={"api_key": TMDB_KEY, "external_source": "imdb_id"},
            impersonate=IMP,
            timeout=10,
        )
        d = r.json()
        for k in ("movie_results", "tv_results"):
            if d.get(k):
                x = d[k][0]
                return (
                    x.get("title") or x.get("name"),
                    x.get("original_title") or x.get("original_name"),
                )
    except Exception:
        pass
    return None, None


def pick_slug_from_search(keywords):
    for kw in keywords:
        if not kw:
            continue
        items = filter_search(api_movies(q=kw), kw)
        if not items:
            continue
        kw_l = kw.lower()
        for it in items:
            t = (it.get("title") or "").lower()
            o = (it.get("original_title") or "").lower()
            if kw_l in t or kw_l in o or t in kw_l or o in kw_l:
                return it.get("slug"), it
        return items[0].get("slug"), items[0]
    return None, None


def stream_title(server_name, info, ep_name=None):
    name = (info or {}).get("title") or (info or {}).get("name") or ""
    origin = (
        (info or {}).get("original_title")
        or (info or {}).get("origin_name")
        or ""
    )
    year = str((info or {}).get("year") or "")[:4]
    nums = re.findall(r"\d+", str(ep_name or ""))
    ep_label = f"Tập {int(nums[0])}" if nums else ""
    parts = [x for x in [name, origin, year, "FHD", "Vietsub", ep_label] if x]
    return f"{server_name}\n" + " - ".join(parts)


@app.get("/manifest.json")
def manifest():
    return j(
        {
            "id": "org.nuvio.onflix.vps",
            "version": "1.1.2",
            "name": "Onflix",
            "description": "Onflix (NC + m3u8)",
            "logo": "https://www.google.com/s2/favicons?domain=https://onflix.lat&sz=256",
            "resources": ["catalog", "meta", "stream"],
            "types": ["movie", "series"],
            "idPrefixes": ["tt", "onflix:"],
            "catalogs": [
                {
                    "id": "onflix_movie",
                    "type": "movie",
                    "name": "Onflix Phim lẻ",
                    "extra": [
                        {"name": "skip", "isRequired": False},
                        {"name": "search", "isRequired": False},
                    ],
                },
                {
                    "id": "onflix_series",
                    "type": "series",
                    "name": "Onflix Phim bộ",
                    "extra": [
                        {"name": "skip", "isRequired": False},
                        {"name": "search", "isRequired": False},
                    ],
                },
                {
                    "id": "onflix_search",
                    "type": "movie",
                    "name": "Onflix Search",
                    "extra": [{"name": "search", "isRequired": True}],
                },
            ],
        }
    )


@app.route("/catalog/<ctype>/<cid>.json")
@app.route("/catalog/<ctype>/<cid>/skip=<int:skip>.json")
@app.route("/catalog/<ctype>/<cid>/search=<path:search>.json")
def catalog(ctype, cid, skip=0, search=None):
    page = max(1, int(skip) // 20 + 1) if skip else 1
    if search:
        q = unquote(search)
        items = filter_search(api_movies(page=1, q=q), q)
    elif "series" in cid or ctype == "series":
        items = api_movies(page=page, type_="phim-bo")
    else:
        items = api_movies(page=page, type_="phim-le")
    return j({"metas": [item_meta(it) for it in items[:40]]})


@app.route("/meta/<mtype>/<path:mid>")
def meta(mtype, mid):
    mid = mid.replace(".json", "")
    if not mid.startswith("onflix:"):
        return j({"meta": {}})
    slug = mid.split(":", 1)[1]
    it = find_by_slug(slug)
    if not it:
        return j(
            {
                "meta": {
                    "id": f"onflix:{slug}",
                    "type": mtype,
                    "name": slug.replace("-", " ").title(),
                }
            }
        )
    out = item_meta(it)
    out["id"] = f"onflix:{slug}"
    eps = parse_episodes(slug)
    videos, seen = [], set()
    for i, ep in enumerate(eps, 1):
        nums = re.findall(r"\d+", str(ep.get("name") or ""))
        epn = int(nums[0]) if nums else i
        if epn in seen:
            continue
        seen.add(epn)
        videos.append(
            {
                "id": f"onflix:{slug}:{epn}",
                "title": f"Tập {epn}",
                "season": 1,
                "episode": epn,
            }
        )
    if len(videos) > 1:
        out["type"] = "series"
        out["videos"] = videos
    return j({"meta": out})


@app.route("/stream/<stype>/<path:sid>")
def stream(stype, sid):
    sid = sid.replace(".json", "")
    episode = None
    slug = None
    info = {}

    if sid.startswith("onflix:"):
        parts = sid.split(":")
        slug = parts[1]
        if len(parts) >= 3:
            try:
                episode = int(parts[-1])
            except ValueError:
                episode = None
        info = find_by_slug(slug) or {"slug": slug, "title": slug.replace("-", " ")}
    else:
        imdb = sid
        if stype == "series" and sid.count(":") >= 2:
            p = sid.split(":")
            imdb = p[0]
            try:
                episode = int(p[2])
            except Exception:
                pass
        title, original = tmdb_names(imdb)
        keywords = []
        for k in (original, title):
            if k and k not in keywords:
                keywords.append(k)
        for k in list(keywords):
            head = re.split(r"[:\-–]", k)[0].strip()
            if head and head not in keywords:
                keywords.append(head)
        slug, info = pick_slug_from_search(keywords)
        if not slug:
            return j({"streams": []})
        info = info or find_by_slug(slug) or {}

    eps = parse_episodes(slug)
    if not eps:
        return j({"streams": []})

    streams = []
    root = public_root()
    for ep in eps:
        if episode is not None and len(eps) > 1:
            nums = re.findall(r"\d+", str(ep.get("name") or ""))
            if nums and int(nums[0]) != int(episode):
                continue
        title = stream_title(
            ep.get("server_name") or "Onflix", info, ep.get("name")
        )
        try:
            if ep.get("kind") == "streamc" and ep.get("link_embed"):
                m3u8 = streamc_m3u8(ep["link_embed"])
                token = (
                    base64.urlsafe_b64encode(ep["link_embed"].encode())
                    .decode()
                    .rstrip("=")
                )
                CACHE[token] = (m3u8, time.time() + 600)
                host = urlparse(ep["link_embed"]).hostname or "embed11.streamc.xyz"
                streams.append(
                    {
                        "name": "Onflix",
                        "title": title,
                        "url": f"{root}/play/{token}.m3u8",
                        "behaviorHints": {
                            "notWebReady": True,
                            "bingeGroup": f"onflix-{slug}",
                            "proxyHeaders": {
                                "request": {
                                    "Referer": f"https://{host}/",
                                    "Origin": f"https://{host}",
                                    "User-Agent": UA,
                                }
                            },
                        },
                    }
                )
            elif ep.get("link_m3u8"):
                streams.append(
                    {
                        "name": "Onflix",
                        "title": title,
                        "url": ep["link_m3u8"],
                        "behaviorHints": {
                            "notWebReady": True,
                            "bingeGroup": f"onflix-{slug}-m3u8",
                            "proxyHeaders": {
                                "request": {
                                    "Referer": SITE + "/",
                                    "User-Agent": UA,
                                }
                            },
                        },
                    }
                )
        except Exception as e:
            streams.append(
                {
                    "name": "Onflix",
                    "title": f"{title}\n{type(e).__name__}",
                    "url": ep.get("link_embed") or ep.get("link_m3u8") or "",
                    "behaviorHints": {"notWebReady": True},
                }
            )
    return j({"streams": streams})


@app.get("/play/<token>.m3u8")
def play(token):
    pad = "=" * ((4 - len(token) % 4) % 4)
    try:
        embed = base64.urlsafe_b64decode(token + pad).decode()
    except Exception:
        return Response("bad token", 400)
    item = CACHE.get(token)
    if item and item[1] > time.time():
        m3u8 = item[0]
    else:
        try:
            m3u8 = streamc_m3u8(embed)
            CACHE[token] = (m3u8, time.time() + 600)
        except Exception as e:
            return Response(str(e), 502)
    return Response(
        m3u8,
        mimetype="application/vnd.apple.mpegurl",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store",
        },
    )


@app.get("/")
def root():
    return j({"ok": True, "version": "1.1.1", "manifest": "/manifest.json"})

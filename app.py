#!/usr/bin/env python3
"""Onflix addon — only primary NC (streamc), no KKPHIM side sources."""
from __future__ import annotations
import base64, hashlib, hmac, json, re, time
from urllib.parse import urljoin, urlparse, unquote
from flask import Flask, Response, request
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from curl_cffi import requests as cfreq

app = Flask(__name__)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
IMP = "chrome120"
API = "https://k8s.onflixcdn.com/api"
SITE = "https://onflix.lat"
PUBLIC_HOST = "168.138.176.147:51823"
TMDB_KEY = "1adf1a2b5aece0ac5106302d3299f56f"
CACHE = {}

def S():
    s = cfreq.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "vi,en;q=0.9", "Referer": SITE + "/"})
    return s

def j(data, code=200):
    return Response(json.dumps(data, ensure_ascii=False), status=code, mimetype="application/json",
                    headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"})

def public_root():
    host = request.headers.get("Host") or PUBLIC_HOST
    if host.startswith("127.") or "localhost" in host:
        host = PUBLIC_HOST
    return f"http://{host}"

def decrypt_bootstrap(envelope, api_url):
    if envelope.get("format") != "aesgcm-v1":
        return envelope
    iv = bytes.fromhex(envelope["iv"])
    data = base64.b64decode(envelope["data"] + "=" * ((4 - len(envelope["data"]) % 4) % 4))
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
        "action": "bootstrap", "request_grant": True,
        "playlist_format": "aesgcm-v2", "bootstrap_format": "aesgcm-v1",
        "pretty_url": True, "path_chunks": True, "referrer": "", "frame_origins": [],
    }
    r = s.post(embed_url, json=body, headers={
        "Content-Type": "application/json", "Referer": embed_url, "Origin": origin,
    }, impersonate=IMP, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"bootstrap {r.status_code}")
    plain = decrypt_bootstrap(r.json(), embed_url)
    pre = plain.get("preissued") or {}
    purl = pre.get("playlist")
    if not purl:
        raise RuntimeError("no playlist")
    r2 = s.get(purl, headers={"Referer": embed_url, "Origin": origin}, impersonate=IMP, timeout=25)
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
    r = S().get(f"{API}/movies", params=params, impersonate=IMP, timeout=15)
    if r.status_code != 200:
        return []
    data = r.json()
    return data.get("data") or []

def parse_page_detail(slug: str):
    """HTML có JSON escaped: episodes với link_embed streamc, src=nc."""
    r = S().get(f"{SITE}/phim/{slug}", impersonate=IMP, timeout=20)
    if r.status_code != 200:
        return None, []
    html = r.text
    # unescape common \\ sequences for search
    # extract episodes array roughly
    m = re.search(r'episodes\\":(\[.*?\])\\",\\"related\\"', html, re.S)
    if not m:
        m = re.search(r'episodes\\":(\[.*?\])\\s*,\\s*\\"related\\"', html, re.S)
    raw = None
    if m:
        raw = m.group(1).encode().decode("unicode_escape")
    else:
        # fallback: find each link_embed near (NC)
        pass
    episodes = []
    if raw:
        try:
            episodes = json.loads(raw)
        except Exception:
            episodes = []
    if not episodes:
        # regex fallback
        for em in re.finditer(
            r'link_embed\\":\\"(https:[^"\\]+)\\".*?link_m3u8\\":\\"(https:[^"\\]+)\\".*?name\\":\\"([^"\\]*)\\".*?server_name\\":\\"([^"\\]*)\\".*?src\\":\\"([^"\\]*)\\"',
            html, re.S,
        ):
            episodes.append({
                "link_embed": em.group(1).encode().decode("unicode_escape") if "\\" in em.group(1) else em.group(1),
                "link_m3u8": em.group(2),
                "name": em.group(3),
                "server_name": em.group(4),
                "src": em.group(5),
            })
        # simpler
        if not episodes:
            embeds = re.findall(r'link_embed\\":\\"(https:\\+/\\+/embed[^"\\]+hash=[a-f0-9]+)\\"', html)
            names = re.findall(r'server_name\\":\\"([^"\\]+)\\"', html)
            srcs = re.findall(r'src\\":\\"([^"\\]+)\\"', html)
            epnames = re.findall(r'\\"name\\":\\"(\d+)\\"', html)
            for i, emb in enumerate(embeds):
                emb = emb.replace("\\/", "/")
                episodes.append({
                    "link_embed": emb,
                    "name": epnames[i] if i < len(epnames) else str(i + 1),
                    "server_name": names[i] if i < len(names) else "Vietsub",
                    "src": srcs[i] if i < len(srcs) else "nc",
                })
    # meta from list fields in page
    title_m = re.search(r'\\"title\\":\\"([^"\\]+)\\"', html)
    origin_m = re.search(r'\\"original_title\\":\\"([^"\\]+)\\"', html)
    year_m = re.search(r'\\"year\\":(\d+)', html)
    poster_m = re.search(r'\\"poster_url\\":\\"(https:[^"\\]+)\\"', html)
    meta = {
        "name": (title_m.group(1) if title_m else slug).encode().decode("unicode_escape") if title_m else slug,
        "origin_name": origin_m.group(1).encode().decode("unicode_escape") if origin_m else "",
        "year": year_m.group(1) if year_m else "",
        "poster": poster_m.group(1).replace("\\/", "/") if poster_m else "",
        "slug": slug,
    }
    return meta, episodes

def is_primary_nc(ep: dict) -> bool:
    src = (ep.get("src") or "").lower()
    sname = (ep.get("server_name") or "").lower()
    if src in ("kk", "kkphim", "ophim", "backup"):
        return False
    if "kkphim" in sname or "ophim" in sname:
        return False
    if src == "nc" or "(nc)" in sname or "nguonc" in sname:
        return True
    # default: allow streamc embeds only
    emb = ep.get("link_embed") or ""
    return "streamc.xyz" in emb or "embed.php?hash=" in emb

def item_meta(it, stype="movie"):
    slug = str(it.get("slug") or it.get("id") or "")
    return {
        "id": f"onflix:{slug}",
        "type": stype if it.get("type") != "phim-bo" else "series",
        "name": it.get("title") or it.get("name") or slug,
        "poster": it.get("poster_url") or it.get("thumb_url") or "",
        "posterShape": "poster",
        "background": it.get("poster_url") or "",
        "description": it.get("categories") or "",
        "releaseInfo": str(it.get("year") or ""),
        "genres": [x.strip() for x in str(it.get("categories") or "").split(",") if x.strip()],
    }

def stream_title(ep, meta, episode=None):
    sname = ep.get("server_name") or "Vietsub"
    # bỏ (NC) cho gọn hoặc giữ
    name = meta.get("name") or ""
    origin = meta.get("origin_name") or meta.get("original_title") or ""
    year = str(meta.get("year") or "")[:4]
    epn = episode
    if epn is None:
        nums = re.findall(r"\d+", str(ep.get("name") or ""))
        epn = int(nums[0]) if nums else None
    ep_label = f"Tập {epn}" if epn is not None else ""
    parts = [x for x in [name, origin, year, "FHD", "Vietsub", ep_label] if x]
    return f"{sname}\n" + " - ".join(parts)

def tmdb_name(imdb_id):
    try:
        r = S().get(f"https://api.themoviedb.org/3/find/{imdb_id}",
                    params={"api_key": TMDB_KEY, "external_source": "imdb_id"}, impersonate=IMP, timeout=10)
        d = r.json()
        for k in ("movie_results", "tv_results"):
            if d.get(k):
                x = d[k][0]
                return x.get("title") or x.get("name"), x.get("original_title") or x.get("original_name")
    except Exception:
        pass
    return None, None

@app.get("/manifest.json")
def manifest():
    return j({
        "id": "org.nuvio.onflix.vps",
        "version": "1.0.0",
        "name": "Onflix",
        "description": "Onflix nguồn chính (NC)",
        "logo": "https://www.google.com/s2/favicons?domain=https://onflix.lat&sz=256",
        "resources": ["catalog", "meta", "stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "onflix:"],
        "catalogs": [
            {"id": "onflix_movie", "type": "movie", "name": "Onflix Phim lẻ",
             "extra": [{"name": "skip", "isRequired": False}]},
            {"id": "onflix_series", "type": "series", "name": "Onflix Phim bộ",
             "extra": [{"name": "skip", "isRequired": False}]},
            {"id": "onflix_search", "type": "movie", "name": "Onflix Search",
             "extra": [{"name": "search", "isRequired": True}]},
        ],
    })

@app.get("/catalog/<ctype>/<cid>.json")
def catalog_basic(ctype, cid):
    return catalog_handler(ctype, cid, None, 1)

@app.get("/catalog/<ctype>/<cid>/search=<path:search>.json")
def catalog_search(ctype, cid, search):
    return catalog_handler(ctype, cid, unquote(search), 1)

@app.get("/catalog/<ctype>/<cid>/skip=<int:skip>.json")
def catalog_skip(ctype, cid, skip):
    return catalog_handler(ctype, cid, None, max(1, skip // 20 + 1))

def catalog_handler(ctype, cid, search, page):
    if search:
        items = api_movies(page=1, q=search)
    elif "series" in cid or ctype == "series":
        items = api_movies(page=page, type_="phim-bo")
    else:
        items = api_movies(page=page, type_="phim-le")
    metas = [item_meta(it) for it in items[:30]]
    return j({"metas": metas})

@app.get("/meta/<mtype>/<path:mid>")
def meta(mtype, mid):
    mid = mid.replace(".json", "")
    if not mid.startswith("onflix:"):
        return j({"meta": {}})
    slug = mid.split(":", 1)[1]
    meta_obj, episodes = parse_page_detail(slug)
    if not meta_obj:
        return j({"meta": {}})
    out = {
        "id": f"onflix:{slug}",
        "type": mtype,
        "name": meta_obj.get("name"),
        "poster": meta_obj.get("poster"),
        "background": meta_obj.get("poster"),
        "releaseInfo": meta_obj.get("year"),
        "description": "",
    }
    # series videos from primary only names
    videos = []
    seen = set()
    for ep in episodes:
        if not is_primary_nc(ep):
            continue
        nums = re.findall(r"\d+", str(ep.get("name") or ""))
        epn = int(nums[0]) if nums else len(videos) + 1
        if epn in seen:
            continue
        seen.add(epn)
        videos.append({"id": f"onflix:{slug}:{epn}", "title": f"Tập {epn}", "season": 1, "episode": epn})
    if len(videos) > 1:
        out["type"] = "series"
        out["videos"] = videos
    return j({"meta": out})

@app.get("/stream/<stype>/<path:sid>")
def stream(stype, sid):
    sid = sid.replace(".json", "")
    season = episode = None
    slug = None
    meta_obj = {}

    if sid.startswith("onflix:"):
        parts = sid.split(":")
        slug = parts[1]
        if len(parts) >= 3:
            episode = int(parts[2])
            season = 1
    else:
        imdb = sid
        if stype == "series" and sid.count(":") >= 2:
            p = sid.split(":")
            imdb, season, episode = p[0], int(p[1]), int(p[2])
        title, original = tmdb_name(imdb)
        items = []
        for kw in [k for k in (original, title) if k]:
            items = api_movies(q=kw)
            if items:
                break
        if not items:
            return j({"streams": []})
        slug = items[0].get("slug")
        meta_obj = {
            "name": items[0].get("title"),
            "origin_name": items[0].get("original_title"),
            "year": items[0].get("year"),
        }

    if not slug:
        return j({"streams": []})
    page_meta, episodes = parse_page_detail(slug)
    if page_meta:
        meta_obj = {**meta_obj, **page_meta}

    streams = []
    root = public_root()
    for ep in episodes:
        if not is_primary_nc(ep):
            continue
        if episode is not None:
            nums = re.findall(r"\d+", str(ep.get("name") or ""))
            if nums and int(nums[0]) != int(episode):
                continue
        embed = ep.get("link_embed") or ""
        if not embed or "hash=" not in embed:
            continue
        title = stream_title(ep, meta_obj, episode)
        try:
            m3u8 = streamc_m3u8(embed)
            token = base64.urlsafe_b64encode(embed.encode()).decode().rstrip("=")
            CACHE[token] = (m3u8, time.time() + 600)
            streams.append({
                "name": "Onflix",
                "title": title,
                "url": f"{root}/play/{token}.m3u8",
                "behaviorHints": {
                    "notWebReady": True,
                    "bingeGroup": f"onflix-{slug}",
                    "proxyHeaders": {
                        "request": {
                            "Referer": "https://embed13.streamc.xyz/",
                            "Origin": "https://embed13.streamc.xyz",
                            "User-Agent": UA,
                        }
                    },
                },
            })
        except Exception as e:
            streams.append({
                "name": "Onflix",
                "title": f"{title}\n{type(e).__name__}",
                "url": embed,
                "behaviorHints": {"notWebReady": True},
            })
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
    return Response(m3u8, mimetype="application/vnd.apple.mpegurl",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

@app.get("/")
def root():
    return j({"ok": True, "name": "Onflix", "manifest": "/manifest.json"})

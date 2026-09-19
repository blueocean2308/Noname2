#!/usr/bin/env python3
"""Onflix VPS 1.3.0 — api/search + NC streamc.
Match Cinemeta/TMDB: ưu tiên title EN, bỏ keyword CJK làm chính, không items[0].
CACHE in-memory TTL 10 phút + prune (không ghi đĩa).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import unicodedata
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
CACHE_TTL = 600
ADDON_VERSION = "1.3.0"

# CJK / Hangul / Hiragana / Katakana
_RE_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


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


def cache_put(token: str, m3u8: str):
    """Lưu playlist decrypt trong RAM; dọn entry hết hạn."""
    now = time.time()
    expired = [k for k, v in CACHE.items() if v[1] <= now]
    for k in expired:
        CACHE.pop(k, None)
    # giới hạn số entry (tránh phình RAM nếu xem nhiều)
    if len(CACHE) > 200:
        for k, _ in sorted(CACHE.items(), key=lambda x: x[1][1])[:50]:
            CACHE.pop(k, None)
    CACHE[token] = (m3u8, now + CACHE_TTL)


def cache_get(token: str):
    item = CACHE.get(token)
    if not item:
        return None
    if item[1] <= time.time():
        CACHE.pop(token, None)
        return None
    return item[0]


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


def api_list(page=1, type_=None):
    params = {"page": page}
    if type_:
        params["type"] = type_
    try:
        r = S().get(f"{API}/movies", params=params, impersonate=IMP, timeout=15)
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("data") or []
    except Exception:
        return []


def api_search(q: str):
    """CloudStream uses /api/search?q= — NOT /api/movies?q=."""
    if not q:
        return []
    try:
        r = S().get(
            f"{API}/search",
            params={"q": q},
            impersonate=IMP,
            timeout=15,
        )
        if r.status_code != 200:
            return []
        d = r.json() or {}
        return d.get("movies") or d.get("data") or []
    except Exception:
        return []


def find_by_slug(slug: str):
    for q in (slug, slug.replace("-", " ")):
        for it in api_search(q):
            if it.get("slug") == slug:
                return it
    for type_ in ("phim-bo", "phim-le", None):
        for page in range(1, 3):
            for it in api_list(page=page, type_=type_):
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
    """Parse NC streamc episodes from HTML (PA/SN/Vietsub)."""
    try:
        r = S().get(f"{SITE}/phim/{slug}", impersonate=IMP, timeout=25)
        if r.status_code != 200:
            return []
        html = r.text
    except Exception:
        return []

    episodes = []
    for m in re.finditer(
        r"link_embed\\\":\\\"(https:[^\"\\]*streamc\.xyz[^\"\\]*hash=[a-f0-9]+)\\\".{0,400}?"
        r"\\\"name\\\":\\\"([^\"\\]*)\\\".{0,120}?"
        r"server_name\\\":\\\"([^\"\\]*)\\\"",
        html,
        re.S,
    ):
        emb = m.group(1).replace("\\/", "/")
        episodes.append(
            {
                "kind": "streamc",
                "link_embed": emb,
                "name": m.group(2),
                "server_name": m.group(3).encode("utf-8").decode("unicode_escape")
                if "\\" in m.group(3)
                else m.group(3),
            }
        )

    if not episodes:
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
                    "server_name": "Vietsub (NC)",
                }
            )

    if not episodes:
        for m in re.finditer(
            r"https:(?:\\+/)+[a-z0-9.-]+(?:\\+/)+[^\s\"'\\]+?\.m3u8",
            html,
            re.I,
        ):
            url = m.group(0).replace("\\/", "/").replace("\\", "")
            if "streamc" in url or "onflixstream" in url or "trailer" in url:
                continue
            episodes.append(
                {
                    "kind": "m3u8",
                    "link_m3u8": url,
                    "name": str(len(episodes) + 1),
                    "server_name": "Vietsub",
                }
            )

    for ep in episodes:
        sn = ep.get("server_name") or ""
        try:
            if "\\u" in sn:
                ep["server_name"] = sn.encode("utf-8").decode("unicode_escape")
        except Exception:
            pass
        try:
            fixed = sn.encode("latin-1").decode("utf-8")
            if fixed != sn:
                ep["server_name"] = fixed
        except Exception:
            pass

    seen, out = set(), []
    for ep in episodes:
        key = (ep.get("link_embed") or ep.get("link_m3u8"), ep.get("name"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ep)
    return out


def normalize_name(s: str) -> str:
    s = (s or "").lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_mostly_cjk(s: str) -> bool:
    if not s:
        return False
    cjk = len(_RE_CJK.findall(s))
    return cjk >= max(1, len(s) // 3)


def score_match(item: dict, title: str, original_title: str, year: str | None) -> int:
    n_title = normalize_name(title)
    n_orig = normalize_name(original_title)
    n_name = normalize_name(item.get("title") or item.get("name") or "")
    n_origin = normalize_name(item.get("original_title") or item.get("origin_name") or "")
    score = 0

    if n_origin and n_orig and n_origin == n_orig:
        score += 120
    if n_origin and n_title and n_origin == n_title:
        score += 120
    if n_name and n_title and n_name == n_title:
        score += 100
    if n_name and n_orig and n_name == n_orig:
        score += 100

    def long_enough(a, b):
        return bool(a and b and min(len(a), len(b)) >= 6)

    if long_enough(n_origin, n_orig) and (n_origin in n_orig or n_orig in n_origin):
        score += 50
    if long_enough(n_origin, n_title) and (n_origin in n_title or n_title in n_origin):
        score += 45
    if long_enough(n_name, n_title) and (n_name in n_title or n_title in n_name):
        score += 40
    if long_enough(n_name, n_orig) and (n_name in n_orig or n_orig in n_name):
        score += 35

    y = str(item.get("year") or "")[:4]
    if year and y and y == str(year)[:4]:
        score += 25
    return score


def tmdb_info(imdb_id: str):
    """TMDB en-US — title tiếng Anh ưu tiên cho Cinemeta."""
    try:
        r = S().get(
            f"https://api.themoviedb.org/3/find/{imdb_id}",
            params={
                "api_key": TMDB_KEY,
                "external_source": "imdb_id",
                "language": "en-US",
            },
            impersonate=IMP,
            timeout=10,
        )
        d = r.json()
        for k in ("movie_results", "tv_results"):
            if d.get(k):
                x = d[k][0]
                title = x.get("title") or x.get("name") or ""
                original = x.get("original_title") or x.get("original_name") or title
                year = (x.get("release_date") or x.get("first_air_date") or "")[:4]
                return title, original, year
    except Exception:
        pass
    return None, None, None


def pick_from_search(title: str, original_title: str, year: str | None):
    """
    Keyword: title EN trước.
    original chỉ dùng nếu không phải CJK (tránh 스캔들 → phim adult).
    Không fallback items[0]. Ngưỡng >= 100.
    """
    keywords = []
    for k in (title, original_title):
        k = (k or "").strip()
        if not k:
            continue
        if is_mostly_cjk(k):
            continue
        if k not in keywords:
            keywords.append(k)
    # nếu cả hai đều CJK — thử original thô 1 lần (không lấy items[0])
    if not keywords and original_title:
        keywords.append(original_title.strip())

    best, best_score = None, 0
    for kw in keywords:
        items = api_search(kw)
        for it in items:
            sc = score_match(it, title or "", original_title or "", year)
            if sc > best_score:
                best_score, best = sc, it
        if best_score >= 120:
            break

    if best and best_score >= 100:
        return best.get("slug"), best
    return None, None


def stream_title(server_name, info, ep_name=None):
    name = (info or {}).get("title") or (info or {}).get("name") or ""
    origin = (info or {}).get("original_title") or ""
    year = str((info or {}).get("year") or "")[:4]
    nums = re.findall(r"\d+", str(ep_name or ""))
    ep_label = f"Tập {int(nums[0])}" if nums else ""
    parts = [x for x in [name, origin, year, "FHD", ep_label] if x]
    return f"{server_name}\n" + " - ".join(parts)


@app.get("/manifest.json")
def manifest():
    return j(
        {
            "id": "org.nuvio.onflix.vps",
            "version": ADDON_VERSION,
            "name": "Onflix",
            "description": "Onflix NC (api/search)",
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
        items = api_search(unquote(search))
    elif "series" in cid or ctype == "series":
        items = api_list(page=page, type_="phim-bo")
    else:
        items = api_list(page=page, type_="phim-le")
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
        info = find_by_slug(slug) or {
            "slug": slug,
            "title": slug.replace("-", " "),
        }
    else:
        imdb = sid
        if stype == "series" and sid.count(":") >= 2:
            p = sid.split(":")
            imdb = p[0]
            try:
                episode = int(p[2])
            except Exception:
                pass
        title, original, year = tmdb_info(imdb)
        if not title and not original:
            return j({"streams": []})
        slug, info = pick_from_search(title or "", original or title or "", year)
        if not slug:
            return j({"streams": []})
        info = info or {}

    eps = parse_episodes(slug)
    if not eps:
        return j({"streams": []})

    streams = []
    root = public_root()
    seen_server = set()
    for ep in eps:
        if episode is not None:
            nums = re.findall(r"\d+", str(ep.get("name") or ""))
            if nums and int(nums[0]) != int(episode):
                continue
        sname = ep.get("server_name") or "Onflix"
        key = sname
        if key in seen_server:
            continue
        seen_server.add(key)
        title = stream_title(sname, info, ep.get("name") if episode else None)
        try:
            if ep.get("kind") == "streamc" and ep.get("link_embed"):
                m3u8 = streamc_m3u8(ep["link_embed"])
                token = (
                    base64.urlsafe_b64encode(ep["link_embed"].encode())
                    .decode()
                    .rstrip("=")
                )
                cache_put(token, m3u8)
                host = (
                    urlparse(ep["link_embed"]).hostname or "embed11.streamc.xyz"
                )
                streams.append(
                    {
                        "name": "Onflix",
                        "title": title,
                        "url": f"{root}/play/{token}.m3u8",
                        "behaviorHints": {
                            "notWebReady": True,
                            "bingeGroup": f"onflix-{slug}-{sname}",
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
                    "url": ep.get("link_embed") or "",
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
    m3u8 = cache_get(token)
    if m3u8 is None:
        try:
            m3u8 = streamc_m3u8(embed)
            cache_put(token, m3u8)
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
    return j({"ok": True, "version": ADDON_VERSION, "manifest": "/manifest.json"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=51823, threaded=True)

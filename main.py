"""QQ 音乐后端（给小手机用）。

每个访问密钥对应一份独立的 QQ 登录态：一个人用就设一个密钥，想给朋友用就多设几个，
每个人各自扫码、各自一份歌单，互不影响。

环境变量：
  ACCESS_KEY       访问密钥，必填。多个密钥用英文逗号隔开，例如 key1,key2
  ALLOWED_ORIGINS  允许的网页来源，逗号分隔，例如 https://你的名字.github.io。不设置则允许所有来源
  DATA_DIR         登录凭证目录，默认 private-data（里面是登录状态，不要公开）

启动：
  ACCESS_KEY=你的密钥 uvicorn main:app --host 127.0.0.1 --port 8788
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from qqmusic_api import Client, Credential
from qqmusic_api.models.login import QRLoginType
from qqmusic_api.modules.login_utils import QRCodeLoginSession
from qqmusic_api.modules.search import SearchType
from qqmusic_api.modules.song import SongFileInfo, SongFileType

DATA = Path(os.environ.get("DATA_DIR", "private-data"))
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

KEYS = []
for _k in os.environ.get("ACCESS_KEY", "").split(","):
    _k = _k.strip()
    if _k and _k not in KEYS:
        KEYS.append(_k)
if not KEYS:
    raise SystemExit("请先设置环境变量 ACCESS_KEY")

# 用来给播放地址签名的服务器密钥（只由你的 ACCESS_KEY 派生，不对外暴露）
SECRET = hashlib.sha256("\n".join(KEYS).encode()).digest()

QUALITIES = (SongFileType.MP3_320, SongFileType.MP3_128, SongFileType.ACC_96)


def khash(key):
    return hashlib.sha256(key.encode()).hexdigest()


class User:
    """一个访问密钥 = 一个用户 = 一份独立的 QQ 登录态。服务器只存密钥的哈希。"""

    def __init__(self, kh, folder):
        self.kh = kh
        self.uid = kh[:16]
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cred_file = folder / "credential.json"
        self.device = str(folder / "device.json")
        self.client = None
        self.cred = None
        self.last_check = 0.0
        self.login_task = None
        self.login_state = "idle"  # idle / starting / waiting / connected / error
        self.login_qr = ""
        self.login_err = ""


USERS = []
BY_ID = {}


def add_user(kh):
    if any(u.kh == kh for u in USERS):
        return None
    u = User(kh, DATA / "users" / kh[:16])
    USERS.append(u)
    BY_ID[u.uid] = u
    return u


for _k in KEYS:
    add_user(khash(_k))

URL_CACHE = {}  # token -> (url, expire_ts)


# ---------------------------------------------------------------- 凭证 / 客户端
def load_cred(u):
    if not u.cred_file.exists():
        return None
    try:
        return Credential.model_validate(json.loads(u.cred_file.read_text(encoding="utf-8")))
    except Exception:
        return None


def save_cred(u, value):
    fd = os.open(u.cred_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value.model_dump(), handle)


async def close_client(u):
    if u.client is not None:
        try:
            await u.client.__aexit__(None, None, None)
        except Exception:
            pass
    u.client = None


async def open_client(u, cred):
    await close_client(u)
    cli = Client(credential=cred, device_path=u.device)
    await cli.__aenter__()
    u.client, u.cred = cli, cred


async def ensure_fresh(u):
    """登录态过期就自动刷新，最多每 30 分钟检查一次。"""
    if not u.client or time.time() - u.last_check < 1800:
        return
    u.last_check = time.time()
    try:
        if await u.client.login.check_expired():
            fresh = await u.client.login.refresh_credential()
            save_cred(u, fresh)
            await open_client(u, fresh)
    except Exception as e:
        print("refresh failed:", u.uid, type(e).__name__)


@asynccontextmanager
async def lifespan(app):
    for u in USERS:
        cred = load_cred(u)
        if cred:
            try:
                await open_client(u, cred)
                await ensure_fresh(u)
                u.login_state = "connected"
            except Exception as e:
                print("open client failed:", u.uid, type(e).__name__, e)
    yield
    for u in USERS:
        await close_client(u)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS or ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    max_age=600,
)


HITS = {}


def throttle(ident, bucket, limit, window):
    now = time.time()
    k = bucket + ":" + ident
    arr = [t for t in HITS.get(k, []) if now - t < window]
    if len(arr) >= limit:
        HITS[k] = arr
        raise HTTPException(429, "操作太频繁，请稍后再试")
    arr.append(now)
    HITS[k] = arr


def auth(x_access_key: str = Header(default="")):
    """校验密钥并返回对应的用户；每个请求只会碰到自己那份登录态。"""
    given = khash(x_access_key)
    found = None
    for u in USERS:
        if hmac.compare_digest(given, u.kh):
            found = u
    if found is None:
        raise HTTPException(401, "bad access key")
    throttle(found.uid, "api", 240, 60)
    return found


async def need(u):
    if u.client is None:
        raise HTTPException(401, "not_logged_in")
    await ensure_fresh(u)
    return u.client


# ---------------------------------------------------------------- 数据整理
def dump(obj):
    try:
        return obj.model_dump()
    except Exception:
        return obj


def norm_song(s):
    singer = s.get("singer") or []
    album = s.get("album") or {}
    f = s.get("file") or {}
    pay = s.get("pay") or {}
    am = album.get("mid") or str(album.get("pmid", "")).split("_")[0]
    return {
        "mid": s.get("mid", ""),
        "id": s.get("id", 0),
        "title": s.get("title") or s.get("name", ""),
        "artist": "、".join((x.get("name") or x.get("title") or "") for x in singer),
        "album": album.get("title") or album.get("name", ""),
        "art": f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{am}.jpg" if am else "",
        "duration": s.get("interval", 0),
        "type": s.get("type", 0),
        "mediaMid": f.get("media_mid", ""),
        "vip": bool(pay.get("pay_play") or pay.get("pay_month")),
    }


def find_songs(obj):
    """在任意结构里找出像歌曲的字典（搜索结果的层级不固定，所以递归找）。"""
    out = []

    def walk(x):
        if isinstance(x, dict):
            if x.get("mid") and (x.get("title") or x.get("name")) and "singer" in x:
                out.append(x)
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return out


# ---------------------------------------------------------------- 基础接口
@app.get("/api/music/health")
async def health():
    return {"ok": True}


@app.get("/api/music/status")
async def status(u: User = Depends(auth)):
    return {"connected": u.client is not None, "musicid": str(getattr(u.cred, "musicid", "")) if u.cred else ""}


# ---------------------------------------------------------------- 扫码登录
async def login_job(u):
    try:
        async with Client(device_path=u.device) as c:
            sess = QRCodeLoginSession(c.login, QRLoginType.MOBILE, timeout_seconds=180)
            qr = await sess.get_qrcode()
            u.login_qr = "data:image/png;base64," + base64.b64encode(qr.data).decode()
            u.login_state = "waiting"
            cred = await sess.wait_qrcode_login()
            save_cred(u, cred)
        await open_client(u, cred)
        u.login_state = "connected"
    except asyncio.CancelledError:
        u.login_state = "idle"
        raise
    except Exception as e:
        u.login_state = "error"
        u.login_err = type(e).__name__


@app.post("/api/music/login/start")
async def login_start(u: User = Depends(auth)):
    throttle(u.uid, "ls", 5, 600)
    if u.login_task and not u.login_task.done():
        u.login_task.cancel()
    u.login_state, u.login_qr, u.login_err = "starting", "", ""
    u.login_task = asyncio.create_task(login_job(u))
    for _ in range(60):
        if u.login_qr or u.login_state == "error":
            break
        await asyncio.sleep(0.25)
    if not u.login_qr:
        raise HTTPException(502, "获取二维码失败：" + (u.login_err or "超时"))
    return {"qr": u.login_qr}


@app.get("/api/music/login/status")
async def login_status(u: User = Depends(auth)):
    state = "connected" if u.client is not None and u.login_state != "waiting" else u.login_state
    return {"state": state, "error": u.login_err}


@app.post("/api/music/logout")
async def logout(u: User = Depends(auth)):
    if u.login_task and not u.login_task.done():
        u.login_task.cancel()
    await close_client(u)
    u.cred = None
    u.login_state = "idle"
    try:
        u.cred_file.unlink()
    except FileNotFoundError:
        pass
    return {"ok": True}


# ---------------------------------------------------------------- 歌单 / 搜索 / 歌词
@app.get("/api/music/playlists")
async def playlists(u: User = Depends(auth)):
    cli = await need(u)
    res = await cli.user.get_created_songlist(int(u.cred.musicid))
    out = []
    for p in res.playlists:
        d = dump(p)
        out.append({
            "id": d.get("id"),
            "title": d.get("title") or d.get("name") or "",
            "songnum": d.get("songnum", 0),
            "cover": d.get("picurl") or d.get("cover") or "",
        })
    return {"playlists": out}


@app.get("/api/music/playlist")
async def playlist(id: int, page: int = 1, num: int = 30, u: User = Depends(auth)):
    cli = await need(u)
    res = await cli.songlist.get_detail(id, num=num, page=page)
    d = dump(res)
    info = d.get("info") or {}
    songs = [norm_song(s) for s in (d.get("songs") or [])]
    total = int(info.get("songnum", 0) or 0)
    return {
        "title": info.get("title", ""),
        "cover": info.get("picurl", ""),
        "total": total,
        "page": page,
        "songs": songs,
        "hasMore": bool(songs) and page * num < total,
    }


@app.get("/api/music/search")
async def search(q: str, page: int = 1, num: int = 20, u: User = Depends(auth)):
    cli = await need(u)
    res = await cli.search.search_by_type(q, SearchType.SONG, num=num, page=page)
    seen, songs = set(), []
    for s in find_songs(dump(res)):
        n = norm_song(s)
        if n["mid"] and n["mid"] not in seen:
            seen.add(n["mid"])
            songs.append(n)
    return {"songs": songs, "page": page}


@app.get("/api/music/lyric")
async def lyric(mid: str, u: User = Depends(auth)):
    cli = await need(u)
    res = await cli.lyric.get_lyric(mid, trans=True)
    d = dump(res)
    return {"lrc": d.get("lyric", ""), "trans": d.get("trans", "")}


# ---------------------------------------------------------------- 播放
async def upstream_url(u, mid, media_mid=None, song_type=0):
    cli = await need(u)
    if not media_mid:
        detail = await cli.song.get_detail(mid)
        track = getattr(detail, "track", None)
        if track is None:
            many = getattr(detail, "tracks", None)
            track = many[0] if many else None
        if track is None:
            raise HTTPException(404, "song not found")
        media_mid = getattr(getattr(track, "file", None), "media_mid", "") or None
        song_type = getattr(track, "type", 0)
    info = SongFileInfo(mid=mid, media_mid=media_mid or None, song_type=song_type or 0)
    for quality in QUALITIES:
        res = await cli.song.get_song_urls(file_info=[info], file_type=quality, credential=u.cred)
        for row in getattr(res, "data", []):
            purl = getattr(row, "purl", "")
            if purl and getattr(row, "result", 0) == 0:
                if purl.startswith("http"):
                    url = purl
                else:
                    sip = getattr(res, "sip", None) or []
                    base = str(sip[0]) if sip else "https://isure.stream.qqmusic.qq.com/"
                    url = base.rstrip("/") + "/" + purl.lstrip("/")
                return url, quality.name
    raise HTTPException(403, "没有可播放的地址（可能需要会员，或该歌曲不可用）")


def make_token(u, mid, media_mid, typ, ttl=6 * 3600):
    payload = json.dumps(
        {"u": u.uid, "m": mid, "mm": media_mid, "t": typ, "e": int(time.time()) + ttl},
        separators=(",", ":"),
    )
    b = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(SECRET, b.encode(), hashlib.sha256).hexdigest()[:32]
    return b + "." + sig


def read_token(token):
    try:
        b, sig = token.split(".", 1)
        good = hmac.new(SECRET, b.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        if data["e"] < time.time() or data["u"] not in BY_ID:
            raise ValueError
        return data
    except Exception:
        raise HTTPException(403, "bad or expired token")


class SongReq(BaseModel):
    mid: str
    mediaMid: str = ""
    type: int = 0


@app.post("/api/music/song")
async def song(req: SongReq, u: User = Depends(auth)):
    url, quality = await upstream_url(u, req.mid, req.mediaMid or None, req.type)
    token = make_token(u, req.mid, req.mediaMid, req.type)
    # direct：让手机直接向 QQ 的音源服务器取音频（手机在国内，不受海外 IP 限制）；url：经本服务器转发（备用）
    return {"url": f"/api/music/stream/{token}", "direct": variants(url)[0], "quality": quality}


UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def fetch_headers(range_header=None):
    h = {"User-Agent": UA, "Referer": "https://y.qq.com/", "Origin": "https://y.qq.com"}
    if range_header:
        h["Range"] = range_header
    return h


def variants(url):
    base = url.split("://", 1)[1] if "://" in url else url
    return ["https://" + base, "http://" + base]


@app.get("/api/music/stream/{token}")
async def stream(token: str, request: Request):
    data = read_token(token)
    u = BY_ID[data["u"]]  # 播放地址只用签发它的那个用户的登录态去取
    now = time.time()
    hit = URL_CACHE.get(token)
    if hit and hit[1] > now:
        url = hit[0]
    else:
        url, _ = await upstream_url(u, data["m"], data.get("mm") or None, data.get("t", 0))
        URL_CACHE[token] = (url, now + 600)
        for k in [k for k, v in URL_CACHE.items() if v[1] < now]:
            URL_CACHE.pop(k, None)
    rng = request.headers.get("range")
    last = "no upstream"
    for v in variants(url):
        http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=True)
        try:
            r = await http.send(http.build_request("GET", v, headers=fetch_headers(rng)), stream=True)
        except Exception as e:
            last = type(e).__name__
            print("stream connect fail", v.split("?")[0], last)
            await http.aclose()
            continue
        if r.status_code >= 400:
            last = f"upstream {r.status_code}"
            print("stream upstream status", r.status_code, v.split("?")[0])
            await r.aclose()
            await http.aclose()
            continue
        out = {k: r.headers[k] for k in ("content-length", "content-range", "accept-ranges") if k in r.headers}
        out.setdefault("accept-ranges", "bytes")
        ct = r.headers.get("content-type", "")
        if not ct or ct.startswith("application/") or "octet" in ct:
            ct = "audio/mp4" if ".m4a" in v.split("?")[0] else "audio/mpeg"
        out["content-type"] = ct

        async def gen(r=r, http=http):
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            finally:
                await r.aclose()
                await http.aclose()

        return StreamingResponse(gen(), status_code=r.status_code, headers=out, media_type=ct)
    URL_CACHE.pop(token, None)
    raise HTTPException(502, last)

#!/usr/bin/env python3
"""
transkript2.py - Akilli YouTube transkript indirici (kanal listesi, otomatik bekleme, hafiza)

KULLANIM
    python3 transkript2.py -f kanallar.txt          # txt dosyasindan tum kanallar
    python3 transkript2.py -t nahamsec @criticalthinkingpodcast
    python3 transkript2.py -s https://youtu.be/XXXXXXXXXXX
    python3 transkript2.py -f kanallar.txt --dry-run   # indirmeden plan goster
    python3 transkript2.py --status                    # hafizadaki durum ozeti
    python3 transkript2.py --diagnose                  # engel var mi? GERCEK olcum

kanallar.txt ornegi (her satir bir kanal/video, # yorum):
    nahamsec
    @criticalthinkingpodcast
    youtube.com@STOKfredrik
    https://www.youtube.com/@jhaddix/videos
    https://youtu.be/dQw4w9WgXcQ        # tek video da olur

CIKTI
    ~/Desktop/transcripts/<Kanal>/<Video Basligi [id]>/
        transcript.txt, transcript_timestamped.txt, transcript.srt, chunks.md,
        metadata.json, description.txt
    ~/Desktop/transcripts/_state.json     hafiza (biten / uyelere-ozel / altyazisiz...)
    ~/Desktop/transcripts/_summary.md     tum kanallarin ozeti + atlanan videolar
    ~/Desktop/transcripts/_errors.jsonl   her hatanin GERCEK kaniti (HTTP kodu, adres, basliklar)
    ~/Desktop/transcripts/transkript2.log

KURULUM
    pip install -U yt-dlp
    pip install faster-whisper      (opsiyonel)
    curl -fsSL https://deno.land/install.sh | sh   (onerilir: YouTube JS dogrulamasi icin)
"""

import argparse
import atexit
import html
import json
import logging
import os
import random
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

try:
    import yt_dlp
    from yt_dlp import YoutubeDL
except ImportError:
    sys.exit("yt-dlp kurulu degil. Kur: python3 -m pip install -U yt-dlp")

log = logging.getLogger("t2")

BUG_BOUNTY_PROMPT = (
    "Bug bounty hacking talk. Terms: SSRF, IDOR, XSS, CSRF, SQLi, RCE, XXE, SSTI, "
    "OAuth, JWT, GraphQL, Burp Suite, ffuf, nuclei, httpx, subfinder, amass, "
    "HackerOne, Bugcrowd, Intigriti, recon, WAF bypass, CORS, subdomain takeover, "
    "NahamSec, Critical Thinking podcast, Justin Gardner, Rhynorater, Joel Margolis."
)

PERMANENT = {"members_only", "private", "unavailable", "age_restricted"}
SKIP_STATUS = {"MEMBERS_ONLY": "members_only", "PRIVATE": "private", "UNAVAILABLE": "unavailable",
               "AGE_RESTRICTED": "age_restricted", "NOT_STARTED": "not_started", "NO_FORMATS": "no_formats"}
STATUS_TR = {"ok": "tamam", "members_only": "uyelere ozel", "private": "ozel", "unavailable": "silinmis/erisilemez",
             "age_restricted": "yas kisitli", "no_subs": "altyazi yok", "not_started": "canli/premiere baslamadi",
             "no_formats": "format yok", "error": "gecici hata"}


# --------------------------------------------------------------------------
# Hata siniflari ve gercek kanit toplama
# --------------------------------------------------------------------------
class Skip(Exception):
    def __init__(self, msg, cat="SKIP"):
        super().__init__(msg)
        self.cat = cat


class Blocked(Exception):
    def __init__(self, msg, cat="HTTP_429", retry_after=None):
        super().__init__(msg)
        self.cat = cat
        self.retry_after = retry_after


class NetDown(Exception):
    pass


class Abort(Exception):
    pass


class CaptureLogger:
    def __init__(self):
        self.lines = deque(maxlen=30)

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.lines.append("W: " + str(msg)[:300])

    def error(self, msg):
        self.lines.append("E: " + str(msg)[:300])
        log.debug("yt-dlp: %s", msg)


CAPTURE = CaptureLogger()
ERR_LOG = {"path": None}


def http_evidence(e):
    ev = {"status": None, "url": None, "retry_after": None, "headers": {}, "body": None}
    seen, stack = set(), [e]
    while stack:
        x = stack.pop()
        if x is None or id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, BaseException):
            stack.extend([x.__cause__, x.__context__])
            ei = getattr(x, "exc_info", None)
            if ei and len(ei) > 1:
                stack.append(ei[1])
            stack.extend(a for a in getattr(x, "args", ()) if isinstance(a, BaseException))
        st = getattr(x, "status", None)
        if st is None:
            st = getattr(x, "code", None)
        if isinstance(st, int) and ev["status"] is None:
            ev["status"] = st
        resp = getattr(x, "response", None)
        if resp is not None:
            try:
                ev["url"] = ev["url"] or getattr(resp, "url", None)
                hd = getattr(resp, "headers", None)
                if hd and not ev["headers"]:
                    for k in ("retry-after", "content-type", "server", "date", "via"):
                        v = hd.get(k)
                        if v:
                            ev["headers"][k] = str(v)[:120]
                if ev["body"] is None:
                    ev["body"] = resp.read()[:300].decode("utf-8", "replace")
            except Exception:  # noqa
                pass
    if ev["status"] is None:
        m = re.search(r"HTTP Error (\d{3})", str(e))
        if m:
            ev["status"] = int(m.group(1))
    ra = ev["headers"].get("retry-after")
    if ra and ra.strip().isdigit():
        ev["retry_after"] = int(ra.strip())
    return ev


def classify(msg, ev):
    """(KOD, aciklama) - sadece kanitlanabilen seyi soyler."""
    low = msg.lower()
    st = ev.get("status")
    host = re.sub(r"^https?://([^/]+).*", r"\1", ev["url"]) if ev.get("url") else "yt-dlp mesajindan alinamadi"
    ra = ev["headers"].get("retry-after", "yok")
    if st == 429 or "too many requests" in low:
        return "HTTP_429", (f"Sunucu HTTP 429 dondurdu (adres: {host}; Retry-After: {ra}). Bu, o istek turu icin "
                            "HIZ SINIRI demektir; IP'ye mi hesaba mi bagli oldugu bu koddan anlasilmaz.")
    if "sign in to confirm" in low or "not a bot" in low:
        return "BOT_CHECK", "YouTube 'bot degilsin' dogrulamasi istedi (yt-dlp mesaji, HTTP kodu yok)."
    if any(k in low for k in ("members-only", "members only", "join this channel")):
        return "MEMBERS_ONLY", "Video kanal uyelerine ozel."
    if "private video" in low:
        return "PRIVATE", "Video ozel."
    if "confirm your age" in low or "age-restricted" in low:
        return "AGE_RESTRICTED", "Yas kisitlamasi: giris yapilmis cerez gerekir."
    if "no video formats found" in low or "requested format is not available" in low:
        return "NO_FORMATS", ("YouTube indirilebilir format dondurmedi (nedeni mesajdan KESIN anlasilmaz). "
                              "Altyazi icin gerekmez; ses indirme (Whisper) icin gerekir.")
    if "live event will begin" in low or "premieres in" in low:
        return "NOT_STARTED", "Canli yayin/premiere henuz baslamadi."
    if any(k in low for k in ("video unavailable", "has been removed", "not available", "terminated",
                              "who has blocked", "copyright", "no longer available")):
        return "UNAVAILABLE", "Video silinmis/erisilemez veya bolgeye kisitli (yt-dlp mesaji)."
    if st and 500 <= st < 600:
        return f"HTTP_{st}", f"Sunucu tarafi hata (adres: {host}). Gecici."
    if st == 403:
        return "HTTP_403", f"Sunucu erisimi reddetti (HTTP 403, adres: {host})."
    if any(k in low for k in ("timed out", "connection", "resolve", "ssl", "network is unreachable", "temporary failure")):
        return "NETWORK", "Ag/baglanti hatasi (internet veya DNS)."
    return "UNKNOWN", "Bilinmeyen hata, ham mesaj _errors.jsonl'a yazildi."


def log_diag(stage, attempt, cat, msg, ev):
    path = ERR_LOG["path"]
    if not path:
        return
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "stage": stage, "attempt": attempt,
           "category": cat, "http_status": ev.get("status"), "url": ev.get("url"),
           "headers": ev.get("headers"), "body_head": ev.get("body"),
           "message": msg[:500], "ytdlp_lines": list(CAPTURE.lines)[-5:]}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa
        pass


def with_retries(fn, what, max_tries=4):
    permanent = {"MEMBERS_ONLY", "PRIVATE", "AGE_RESTRICTED", "NO_FORMATS", "UNAVAILABLE", "NOT_STARTED"}
    for attempt in range(1, max_tries + 1):
        try:
            return fn()
        except KeyboardInterrupt:
            raise
        except (Skip, Blocked, NetDown, Abort):
            raise
        except Exception as e:  # noqa
            msg = str(e)
            ev = http_evidence(e)
            cat, why = classify(msg, ev)
            log_diag(what, attempt, cat, msg, ev)
            if cat in permanent:
                raise Skip(f"[{cat}] {why}", cat)
            blocked = cat in ("HTTP_429", "BOT_CHECK")
            limit = 2 if blocked else max_tries      # engelde israr etme, ust katman akilli bekler
            if attempt >= limit:
                if blocked:
                    raise Blocked(f"[{cat}] {why}", cat, ev.get("retry_after"))
                if cat == "NETWORK":
                    raise NetDown(why)
                raise RuntimeError(f"[{cat}] {why} | ham: {msg.splitlines()[-1][:150]}")
            wait = min(ev["retry_after"], 120) if (blocked and ev.get("retry_after")) else (
                20 + random.uniform(0, 5) if blocked else 4 * 2 ** attempt + random.uniform(0, 3))
            log.warning("%s: [%s] deneme %d/%d -> %.0fs sonra tekrar", what, cat, attempt, limit, wait)
            time.sleep(wait)


# --------------------------------------------------------------------------
# Genel yardimcilar
# --------------------------------------------------------------------------
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize(name, maxlen=100):
    name = unicodedata.normalize("NFKC", str(name or ""))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" .")
    if name.upper() in RESERVED:
        name = f"_{name}"
    return name or "untitled"


def desktop_dir():
    home = Path.home()
    for c in (home / "Desktop", home / "OneDrive" / "Desktop", home / "Masaüstü"):
        if c.is_dir():
            return c
    d = home / "Desktop"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fmt_ts(sec, srt=False):
    sec = max(0.0, float(sec))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if srt:
        ms = int(round((sec - int(sec)) * 1000))
        if ms == 1000:
            ms, s = 0, s + 1
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def atomic_write(path, text):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def in_future(iso):
    try:
        return datetime.fromisoformat(iso) > datetime.now()
    except Exception:  # noqa
        return False


def extract_video_id(s):
    s = s.strip()
    if re.fullmatch(r"[\w-]{11}", s):
        return s
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})", s)
    return m.group(1) if m else None


def channel_base_url(t):
    t = t.strip()
    if t.startswith("http"):
        return re.sub(r"/(videos|streams|shorts|featured|playlists|podcasts|live|about|community)/?$", "", t.rstrip("/"))
    handle = t.lstrip("@").replace(" ", "")
    if re.fullmatch(r"UC[\w-]{22}", handle):
        return f"https://www.youtube.com/channel/{handle}"
    return f"https://www.youtube.com/@{handle}"


def parse_target(raw):
    """Bir satiri {'kind': channel|video|playlist, 'value': ...} olarak yorumlar."""
    t = (raw or "").strip()
    if not t or t.startswith("#"):
        return None
    t = re.sub(r"\s+#.*$", "", t).strip().strip(",;")
    if not t:
        return None
    t = re.sub(r"(youtube\.com)(?=@)", r"\1/", t, flags=re.I)          # youtube.com@nahamsec
    if re.match(r"^(www\.|m\.|music\.)?(youtube\.com|youtu\.be)\b", t, re.I):
        t = "https://" + t
    if t.lower().startswith("http"):
        low = t.lower()
        if "/playlist" in low and "list=" in low:
            return {"kind": "playlist", "value": t}
        vid = extract_video_id(t) if re.search(r"(watch\?|youtu\.be/|/shorts/|/live/|/embed/)", low) else None
        if vid:
            return {"kind": "video", "value": vid}
        return {"kind": "channel", "value": t}
    m = re.fullmatch(r"v:([\w-]{11})", t)
    if m:
        return {"kind": "video", "value": m.group(1)}
    if re.fullmatch(r"[\w-]{11}", t) and re.search(r"[A-Z0-9_-]", t):
        return {"kind": "video", "value": t}
    return {"kind": "channel", "value": t}


def load_targets(args):
    raw = []
    for t in args.channel or []:
        raw.append(t)
    for s in args.single or []:
        raw.append(f"v:{extract_video_id(s)}" if extract_video_id(s) else s)
    for fp in args.file or []:
        p = Path(fp).expanduser()
        if not p.exists():
            sys.exit(f"Dosya bulunamadi: {p}")
        raw.extend(p.read_text(encoding="utf-8-sig").splitlines())
    out, seen = [], set()
    for r in raw:
        t = parse_target(r)
        if not t:
            continue
        key = (t["kind"], t["value"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def pid_alive(pid):
    if os.name == "nt":
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_lock(root):
    p = root / ".transkript2.lock"
    if p.exists():
        try:
            pid = int(p.read_text().strip())
        except Exception:  # noqa
            pid = None
        if pid and pid != os.getpid() and pid_alive(pid):
            sys.exit(f"Baska bir kopya zaten calisiyor (PID {pid}). Ayni anda iki kopya = daha fazla 429. "
                     f"Durdur: kill {pid}   (eski kilit ise sil: {p})")
    p.write_text(str(os.getpid()))
    atexit.register(lambda: p.unlink(missing_ok=True))


# --------------------------------------------------------------------------
# Hafiza (state)
# --------------------------------------------------------------------------
class State:
    def __init__(self, root):
        self.root = root
        self.path = root / "_state.json"
        self.data = {"videos": {}, "channels": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:  # noqa
                shutil.copy(self.path, self.path.with_suffix(".json.bak"))
                log.warning("_state.json bozuk, yedeklendi; klasorlerden yeniden olusturuluyor.")
        self.data.setdefault("videos", {})
        self.data.setdefault("channels", {})
        self.v = self.data["videos"]

    def save(self):
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False))

    def set(self, vid, **kw):
        e = self.v.setdefault(vid, {})
        e.update(kw)
        e["updated"] = now_iso()
        return e

    def skip_reason(self, vid, args):
        """None = islenmeli; metin = neden atlandigi."""
        e = self.v.get(vid)
        if not e or args.force:
            return None
        st = e.get("status")
        if st == "ok":
            return "ok"
        if args.retry_failed:
            return None
        if st in PERMANENT:
            if st == "age_restricted" and (args.cookies or args.cookies_from_browser) and not e.get("tried_cookies"):
                return None
            return st
        if st in ("no_subs", "not_started", "no_formats") and in_future(e.get("retry_after", "")):
            return st
        return None

    def scan_existing(self):
        n = 0
        for ch in self.root.iterdir():
            if not ch.is_dir() or ch.name.startswith(("_", ".")):
                continue
            for d in ch.iterdir():
                m = re.search(r"\[([\w-]{11})\]$", d.name)
                if not (d.is_dir() and m and (d / "metadata.json").exists()):
                    continue
                vid = m.group(1)
                if self.v.get(vid, {}).get("status") == "ok":
                    continue
                try:
                    meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
                except Exception:  # noqa
                    meta = {}
                self.set(vid, status="ok", title=meta.get("title") or d.name, channel=ch.name,
                         dir=str(d.relative_to(self.root)), source=meta.get("transcript_source"),
                         suspect=bool(meta.get("suspect")), words=meta.get("words"))
                n += 1
        if n:
            log.info("Diskten %d onceden indirilmis video hafizaya eklendi.", n)
            self.save()


# --------------------------------------------------------------------------
# yt-dlp ayarlari, listeleme
# --------------------------------------------------------------------------
def base_opts(args):
    o = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": CAPTURE,
         "retries": 3, "fragment_retries": 3, "socket_timeout": 30, "ignoreerrors": False,
         "ignore_no_formats_error": True}
    if args.cookies:
        o["cookiefile"] = str(Path(args.cookies).expanduser())
    if args.cookies_from_browser:
        o["cookiesfrombrowser"] = (args.cookies_from_browser,)
    return o


def flatten_entries(info):
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        if e.get("entries"):
            yield from flatten_entries(e)
        elif e.get("id"):
            yield e


def list_channel(ctx, target):
    args = ctx.args
    base = channel_base_url(target)
    tabs = ["videos"] + ([] if args.no_streams else ["streams"]) + (["shorts"] if args.shorts else [])
    seen, items, name = set(), [], None
    for tab in tabs:
        url = f"{base}/{tab}"
        opts = {**base_opts(args), "extract_flat": "in_playlist", "skip_download": True}

        def go():
            with YoutubeDL(opts) as y:
                return y.extract_info(url, download=False)
        try:
            info = with_retries(go, f"liste[{tab}]")
        except Skip as e:
            log.info("   %s sekmesi atlandi: %s", tab, e)
            continue
        except (Blocked, NetDown):
            raise
        except Exception as e:  # noqa
            if tab == "videos":
                raise
            log.info("   %s sekmesi yok/okunamadi", tab)
            continue
        name = name or info.get("channel") or info.get("uploader") or info.get("title")
        n = 0
        for e in flatten_entries(info):
            if e["id"] in seen or not re.fullmatch(r"[\w-]{11}", e["id"]):
                continue
            seen.add(e["id"])
            items.append({"id": e["id"], "title": e.get("title") or e["id"], "tab": tab})
            n += 1
        log.info("   %s: %d video", tab, n)
    name = re.sub(r"\s*-\s*(Videos|Live|Shorts)$", "", name or "", flags=re.I)
    return name or target.lstrip("@"), items


def list_playlist(ctx, url):
    opts = {**base_opts(ctx.args), "extract_flat": "in_playlist", "skip_download": True}

    def go():
        with YoutubeDL(opts) as y:
            return y.extract_info(url, download=False)
    info = with_retries(go, "playlist")
    items = [{"id": e["id"], "title": e.get("title") or e["id"]} for e in flatten_entries(info)
             if re.fullmatch(r"[\w-]{11}", e["id"])]
    return info.get("title") or "playlist", items


# --------------------------------------------------------------------------
# Transkript isleme
# --------------------------------------------------------------------------
def parse_json3_text(txt):
    data = json.loads(txt)
    segs = []
    for ev in data.get("events", []):
        if "segs" not in ev:
            continue
        text = html.unescape("".join(s.get("utf8", "") for s in ev["segs"])).replace("\n", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        start = ev.get("tStartMs", 0) / 1000.0
        end = start + ev.get("dDurationMs", 0) / 1000.0
        if segs and segs[-1]["text"] == text:
            segs[-1]["end"] = max(segs[-1]["end"], end)
            continue
        segs.append({"start": start, "end": end, "text": text})
    segs.sort(key=lambda s: s["start"])
    for i, s in enumerate(segs):
        nxt = segs[i + 1]["start"] if i + 1 < len(segs) else None
        if nxt is not None and s["end"] > nxt:
            s["end"] = nxt
        if s["end"] <= s["start"]:
            s["end"] = s["start"] + 1.0
    return segs


def paragraphs(segs, max_span=45.0, gap=2.5):
    out, cur, p_start, last_end = [], [], 0.0, 0.0
    for s in segs:
        if cur and (s["start"] - last_end > gap or s["end"] - p_start > max_span):
            out.append((p_start, " ".join(cur)))
            cur = []
        if not cur:
            p_start = s["start"]
        cur.append(s["text"])
        last_end = s["end"]
    if cur:
        out.append((p_start, " ".join(cur)))
    return out


def build_srt(segs):
    return "\n".join(f"{i}\n{fmt_ts(s['start'], True)} --> {fmt_ts(s['end'], True)}\n{s['text']}\n"
                     for i, s in enumerate(segs, 1))


def build_chunks(segs, title, url, minutes=10):
    size = minutes * 60
    buckets = {}
    for s in segs:
        buckets.setdefault(int(s["start"] // size), []).append(s)
    lines = [f"# {title}", url, ""]
    for k in sorted(buckets):
        lines += [f"## [{fmt_ts(k * size)} - {fmt_ts((k + 1) * size)}]", ""]
        for t, para in paragraphs(buckets[k], max_span=60):
            lines += [f"[{fmt_ts(t)}] {para}", ""]
    return "\n".join(lines)


def looks_suspect(segs, duration):
    if not duration or duration < 180:
        return False
    words = sum(len(s["text"].split()) for s in segs)
    return words / (duration / 60.0) < 25


def pick_lang(avail, langs, auto):
    for l in langs:
        if auto:
            for cand in (f"{l}-orig", l):
                if cand in avail:
                    return cand
        elif l in avail:
            return l
    for l in langs:
        pref = l.split("-")[0]
        for k in avail:
            if k == pref or (not auto and k.startswith(pref + "-")):
                return k
    return None


def save_outputs(out_dir, info, segs, source, lang, suspect):
    out_dir.mkdir(parents=True, exist_ok=True)
    vid, title = info["id"], info.get("title") or info["id"]
    url = f"https://www.youtube.com/watch?v={vid}"
    paras = paragraphs(segs)
    header = f"{title}\n{url}\n\n"
    atomic_write(out_dir / "transcript.txt", header + "\n\n".join(p for _, p in paras) + "\n")
    atomic_write(out_dir / "transcript_timestamped.txt",
                 header + "\n\n".join(f"[{fmt_ts(t)}] {p}" for t, p in paras) + "\n")
    atomic_write(out_dir / "transcript.srt", build_srt(segs))
    atomic_write(out_dir / "chunks.md", build_chunks(segs, title, url))
    if info.get("description"):
        atomic_write(out_dir / "description.txt", info["description"])
    meta = {"id": vid, "title": title, "url": url, "channel": info.get("channel") or info.get("uploader"),
            "upload_date": info.get("upload_date"), "duration_sec": info.get("duration") or 0,
            "view_count": info.get("view_count"), "was_live": info.get("was_live"),
            "tags": info.get("tags"), "chapters": info.get("chapters"),
            "transcript_source": source, "language": lang, "suspect": suspect,
            "segments": len(segs), "words": sum(len(s["text"].split()) for s in segs),
            "downloaded_at": now_iso(), "status": "ok"}
    atomic_write(out_dir / "metadata.json", json.dumps(meta, ensure_ascii=False, indent=2))
    return meta


_whisper_model = None


def whisper_available():
    try:
        import faster_whisper  # noqa
        return True
    except ImportError:
        return False


def whisper_transcribe(ctx, url, lang_hint):
    global _whisper_model
    from faster_whisper import WhisperModel
    args = ctx.args
    tmp = Path(tempfile.mkdtemp(prefix="t2_"))
    try:
        opts = {**base_opts(args), "format": "bestaudio/best", "outtmpl": str(tmp / "audio.%(ext)s")}

        def go():
            with YoutubeDL(opts) as y:
                y.download([url])
        with_retries(go, "ses indirme")
        audio = next(iter(sorted(tmp.glob("audio.*"))), None)
        if not audio:
            raise RuntimeError("ses dosyasi indirilemedi")
        if _whisper_model is None:
            log.info("   Whisper modeli yukleniyor (%s)...", args.whisper_model)
            _whisper_model = WhisperModel(args.whisper_model, device="auto", compute_type="auto")
        lang = (lang_hint or args.lang.split(",")[0]).split("-")[0] or None
        it, _ = _whisper_model.transcribe(str(audio), language=lang, vad_filter=True, beam_size=5,
                                          initial_prompt=BUG_BOUNTY_PROMPT, condition_on_previous_text=False)
        segs = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in it if s.text.strip()]
        return segs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def get_info(ctx, vid):
    url = f"https://www.youtube.com/watch?v={vid}"
    info = with_retries(lambda: ctx.y.extract_info(url, download=False), "video bilgisi")
    if info.get("live_status") in ("is_live", "is_upcoming"):
        raise Skip("canli yayin/premiere henuz bitmedi", "NOT_STARTED")
    return info


def fetch_subs(ctx, info):
    """Altyazi adresini DOGRUDAN indirir (ek bilgi istegi yok). (segs, source, lang) | None"""
    args = ctx.args
    langs = [l.strip() for l in args.lang.split(",") if l.strip()]
    man, aut = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    cands = []
    k = pick_lang(man, langs, False)
    if k:
        cands.append((k, False, man))
    if not args.manual_only:
        k = pick_lang(aut, langs, True)
        if k:
            cands.append((k, True, aut))
    for lang, is_auto, d in cands:
        ent = next((e for e in (d.get(lang) or []) if e.get("ext") == "json3"), None)
        if not ent:
            continue

        def go():
            return ctx.y.urlopen(ent["url"]).read().decode("utf-8", "replace")
        txt = with_retries(go, "altyazi")
        if not txt.strip():
            vid = info["id"]
            if vid in ctx.empty_seen:
                log.info("   altyazi yaniti iki kez bos (HTTP 200) -> altyazisiz sayiliyor")
                return None
            ctx.empty_seen.add(vid)
            raise Blocked("[EMPTY_200] Altyazi adresi HTTP 200 dondurdu ama govde BOS (hiz siniri belirtisi olabilir).",
                          "EMPTY_200")
        segs = parse_json3_text(txt)
        if segs:
            return segs, ("auto" if is_auto else "manual"), lang
    return None


def out_dir_for(ctx, info, channel_dir):
    e = ctx.state.v.get(info["id"], {})
    if e.get("dir") and (ctx.root / e["dir"]).is_dir():
        return ctx.root / e["dir"]
    cdir = channel_dir or ctx.root / sanitize(info.get("channel") or info.get("uploader") or "unknown_channel")
    return cdir / f"{sanitize(info.get('title') or info['id'], 90)} [{info['id']}]"


def process_subs(ctx, vid, channel_dir):
    info = get_info(ctx, vid)
    got = fetch_subs(ctx, info)
    out_dir = out_dir_for(ctx, info, channel_dir)
    ch = out_dir.parent.name
    if not got:
        ctx.state.set(vid, status="no_subs", title=info.get("title"), channel=ch,
                      retry_after=(datetime.now() + timedelta(days=3)).isoformat(timespec="seconds"))
        return "no_subs", None
    segs, source, lang = got
    suspect = source == "auto" and looks_suspect(segs, info.get("duration") or 0)
    meta = save_outputs(out_dir, info, segs, source, lang, suspect)
    ctx.state.set(vid, status="ok", title=meta["title"], channel=ch, dir=str(out_dir.relative_to(ctx.root)),
                  source=source, suspect=suspect, words=meta["words"], tried_cookies=bool(ctx.cookies))
    return "ok", meta


def process_whisper(ctx, vid):
    info = get_info(ctx, vid)
    segs = whisper_transcribe(ctx, f"https://www.youtube.com/watch?v={vid}", info.get("language"))
    if not segs:
        raise Skip("whisper bos sonuc dondurdu", "NO_FORMATS")
    out_dir = out_dir_for(ctx, info, None)
    meta = save_outputs(out_dir, info, segs, "whisper", info.get("language") or ctx.args.lang, False)
    ctx.state.set(vid, status="ok", title=meta["title"], channel=out_dir.parent.name,
                  dir=str(out_dir.relative_to(ctx.root)), source="whisper", suspect=False,
                  words=meta["words"], whisper_retry_after=None)
    return meta


# --------------------------------------------------------------------------
# Saglik kontrolu, bekleme, hiz ayari
# --------------------------------------------------------------------------
def probe(y, url, read=True):
    out = {"ok": False, "status": None, "final_url": None, "headers": {}, "body": "", "error": None}
    try:
        r = y.urlopen(url)
        out.update(ok=True, status=getattr(r, "status", None) or r.getcode())
        out["final_url"] = getattr(r, "url", None)
        for k in ("retry-after", "content-type", "server", "date"):
            v = r.headers.get(k)
            if v:
                out["headers"][k] = str(v)[:100]
        if read:
            out["body"] = r.read(300000).decode("utf-8", "replace")
    except Exception as e:  # noqa
        ev = http_evidence(e)
        out.update(status=ev["status"], final_url=ev["url"], headers=ev["headers"],
                   body=ev["body"] or "", error=str(e).splitlines()[-1][:200])
    return out


def health_report(y, args, video):
    """(ok, [gercek olcum satirlari]). Hicbir sey tahmin etmez."""
    lines, ok = [], True
    url = f"https://www.youtube.com/watch?v={video}"
    w = probe(y, url)
    body = w["body"] or ""
    marks = [n for m, n in (("/sorry/", "captcha sayfasi"), ("unusual traffic", "olagandisi trafik uyarisi"),
                            ("Sign in to confirm", "bot dogrulamasi")) if m in body or m in (w["final_url"] or "")]
    lines.append(f"izleme sayfasi: HTTP {w['status']} | isaretler: {', '.join(marks) or 'yok'}"
                 + (f" | hata: {w['error']}" if w["error"] else ""))
    if w["status"] != 200 or marks:
        return False, lines
    try:
        info = y.extract_info(url, download=False)
    except Exception as e:  # noqa
        ev = http_evidence(e)
        cat, why = classify(str(e), ev)
        lines.append(f"yt-dlp bilgi: HATA [{cat}] HTTP {ev['status']} - {why}")
        return False, lines
    fm = info.get("formats") or []
    media = [f for f in fm if f.get("vcodec") not in (None, "none") or f.get("acodec") not in (None, "none")]
    lines.append(f"yt-dlp bilgi: OK | availability={info.get('availability')} | formatlar: toplam {len(fm)}, "
                 f"video/ses {len(media)}")
    langs = [l.strip() for l in args.lang.split(",") if l.strip()]
    man, aut = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    k = pick_lang(man, langs, False)
    d = man
    if not k:
        k, d = pick_lang(aut, langs, True), aut
    ent = next((e for e in (d.get(k) or []) if e.get("ext") == "json3"), None) if k else None
    if not ent:
        lines.append("altyazi testi: bu videoda istenen dilde json3 altyazi yok (test yapilamadi)")
        return True, lines
    sp = probe(y, ent["url"])
    n = len(sp["body"] or "")
    ra = sp["headers"].get("retry-after")
    lines.append(f"altyazi adresi: HTTP {sp['status']} | govde {n} bayt" + (f" | Retry-After: {ra}" if ra else "")
                 + (f" | hata: {sp['error']}" if sp["error"] else ""))
    if not sp["ok"] or n == 0:
        if sp["body"]:
            lines.append(f"sunucu yanit basi: {sp['body'][:200]!r}")
        return False, lines
    return True, lines


def sleep_long(seconds, label):
    end = datetime.now() + timedelta(seconds=seconds)
    log.info("   %s: %d dk bekleniyor (bitis ~%s). Durdurmak: Ctrl+C", label, round(seconds / 60), end.strftime("%H:%M"))
    left = seconds
    while left > 0:
        step = min(300, left)
        time.sleep(step)
        left -= step
        if left > 0:
            log.info("      ...%d dk kaldi", round(left / 60))


def cooldown(ctx, exc):
    args = ctx.args
    log.warning("   ENGEL: %s", exc)
    while True:
        if ctx.block_level > args.max_blocks:
            log.error("Cok fazla engel (%d). Birkac saat sonra tekrar calistir; kaldigi yerden devam eder.",
                      ctx.block_level - 1)
            return False
        base = min(args.block_wait * 60 * min(ctx.block_level, 3), args.block_max * 60)
        wait = max(getattr(exc, "retry_after", None) or 0, base)
        sleep_long(wait, f"Soguma #{ctx.block_level}")
        ok, lines = health_report(ctx.y, args, args.health_video)
        for l in lines:
            log.info("   saglik: %s", l)
        if ok:
            log.info("   Engel kalkti, devam ediliyor.")
            return True
        ctx.block_level += 1
        exc = Blocked("saglik kontrolu hala basarisiz", "STILL_BLOCKED")


def wait_network(ctx):
    log.warning("   Internet/DNS sorunu. Baglanti bekleniyor (en fazla %d dk)...", ctx.args.net_wait)
    deadline = time.time() + ctx.args.net_wait * 60
    while time.time() < deadline:
        time.sleep(30)
        if probe(ctx.y, "https://www.youtube.com/", read=False)["status"]:
            log.info("   Baglanti geri geldi.")
            return True
    return False


def guarded(ctx, fn):
    """Engel / ag kopmasi olursa akilli bekleyip ayni isi tekrar dener."""
    while True:
        try:
            return fn()
        except Blocked as e:
            ctx.block_level += 1
            ctx.pacer.on_block()
            if not cooldown(ctx, e):
                raise Abort()
        except NetDown:
            if not wait_network(ctx):
                raise Abort()


class Pacer:
    def __init__(self, args):
        self.lo, self.hi, self.mult, self.ok = args.sleep_min, args.sleep_max, 1.0, 0

    def wait(self):
        time.sleep(random.uniform(self.lo, self.hi) * self.mult)

    def on_block(self):
        self.mult = min(self.mult * 1.5, 6.0)
        log.info("   Hiz yavaslatildi (carpan x%.1f)", self.mult)

    def on_success(self):
        self.ok += 1
        if self.ok % 25 == 0 and self.mult > 1.0:
            self.mult = max(1.0, self.mult / 1.25)


class Ctx:
    pass


# --------------------------------------------------------------------------
# Is akisi
# --------------------------------------------------------------------------
def eta_text(ctx, remaining):
    if not ctx.times or not remaining:
        return ""
    avg = sum(ctx.times) / len(ctx.times)
    return f" | tahmini kalan ~{round(avg * remaining / 60)} dk"


def run_queue(ctx, items, channel_dir, label):
    stats = {"ok": 0, "no_subs": 0, "skipped": 0, "failed": 0}
    state, args = ctx.state, ctx.args
    pending = []
    for it in items:
        r = state.skip_reason(it["id"], args)
        if r:
            stats["skipped"] += 1
        else:
            pending.append(it)
    log.info("   toplam %d | atlanan (tamam/kalici/beklemede): %d | islenecek: %d",
             len(items), stats["skipped"], len(pending))
    if args.dry_run:
        return stats
    for i, it in enumerate(pending, 1):
        vid, title = it["id"], it.get("title") or it["id"]
        ctx.seen_ids.add(vid)
        log.info("[%s %d/%d] %s (%s)%s", label, i, len(pending), title[:70], vid, eta_text(ctx, len(pending) - i))
        t0 = time.time()
        try:
            status, meta = guarded(ctx, lambda: process_subs(ctx, vid, channel_dir))
            ctx.block_level = 0
            ctx.pacer.on_success()
            if status == "ok":
                stats["ok"] += 1
                note = " (SUPHELI, whisper ile iyilestirilebilir)" if meta["suspect"] else ""
                log.info("   OK [%s] %d kelime%s", meta["transcript_source"], meta["words"], note)
            else:
                stats["no_subs"] += 1
                log.info("   altyazi yok -> hafizaya yazildi (3 gun sonra tekrar denenir)")
        except Skip as e:
            stats["failed"] += 1
            st = SKIP_STATUS.get(e.cat, "error")
            kw = {"status": st, "title": title, "note": str(e)[:200], "tried_cookies": bool(ctx.cookies)}
            if st in ("not_started", "no_formats"):
                kw["retry_after"] = (datetime.now() + timedelta(days=1 if st == "not_started" else 7)).isoformat(timespec="seconds")
            state.set(vid, **kw)
            log.warning("   atlandi -> hafizaya yazildi (%s): %s", STATUS_TR.get(st, st), e)
        except Abort:
            state.save()
            raise
        except KeyboardInterrupt:
            state.save()
            raise
        except Exception as e:  # noqa
            stats["failed"] += 1
            msg = str(e).splitlines()[-1][:200] if str(e) else repr(e)
            state.set(vid, status="error", title=title, note=msg)
            log.error("   gecici hata (sonraki calistirmada tekrar denenir): %s", msg)
            log.debug("detay", exc_info=True)
        state.save()
        ctx.times.append(time.time() - t0 + 0.0)
        if i < len(pending):
            ctx.pacer.wait()
    return stats


def process_channel(ctx, t, idx, total):
    args, state = ctx.args, ctx.state
    log.info("\n=== [%d/%d] %s: %s ===", idx, total, t["kind"], t["value"])
    base = channel_base_url(t["value"])
    cache = state.data["channels"].get(base)
    fresh = cache and (datetime.now() - datetime.fromisoformat(cache["listed_at"])) < timedelta(hours=args.list_cache_hours)
    if fresh and not args.refresh:
        name, items = cache["name"], cache["items"]
        log.info("   liste onbellekten (%d video, yas: %s)", len(items), cache["listed_at"])
    else:
        name, items = guarded(ctx, lambda: list_channel(ctx, t["value"]))
        state.data["channels"][base] = {"name": name, "items": items, "listed_at": now_iso()}
        state.save()
    if args.order == "oldest":
        items = list(reversed(items))
    if args.limit:
        items = items[:args.limit]
    cdir = ctx.root / sanitize(name)
    cdir.mkdir(parents=True, exist_ok=True)
    log.info("   Kanal: %s -> %s", name, cdir)
    stats = run_queue(ctx, items, cdir, f"{idx}/{total}")
    write_index(ctx, cdir)
    log.info("   Kanal ozeti: %s", stats)
    return stats


def process_loose(ctx, items, label, channel_dir=None):
    stats = run_queue(ctx, items, channel_dir, label)
    for ch in {Path(e["dir"]).parts[0] for v, e in ctx.state.v.items() if v in ctx.seen_ids and e.get("dir")}:
        write_index(ctx, ctx.root / ch)
    return stats


def wants_whisper(ctx, vid, e):
    args = ctx.args
    if e.get("whisper_retry_after") and in_future(e["whisper_retry_after"]):
        return False
    if e.get("status") in ("no_subs",):
        return True
    if e.get("status") == "ok" and e.get("source") != "whisper":
        return bool(e.get("suspect")) or args.whisper == "always"
    return False


def whisper_pass(ctx):
    args, state = ctx.args, ctx.state
    if args.whisper == "off" or args.dry_run:
        return
    cands = [v for v in list(ctx.seen_ids) if wants_whisper(ctx, v, state.v.get(v, {}))]
    if not cands:
        return
    if not whisper_available():
        log.info("\nWhisper'a uygun %d video var ama faster-whisper kurulu degil (pip install faster-whisper).", len(cands))
        return
    log.info("\n=== WHISPER GECISI: %d video (altyazisiz/supheli) ===", len(cands))
    for i, vid in enumerate(cands, 1):
        log.info("[whisper %d/%d] %s (%s)", i, len(cands), state.v[vid].get("title", vid)[:70], vid)
        try:
            meta = guarded(ctx, lambda: process_whisper(ctx, vid))
            log.info("   OK [whisper] %d kelime", meta["words"])
        except Skip as e:
            state.set(vid, whisper_retry_after=(datetime.now() + timedelta(days=7)).isoformat(timespec="seconds"),
                      note=f"whisper: {e}"[:200])
            log.warning("   whisper atlandi (7 gun sonra tekrar): %s", e)
        except (Abort, KeyboardInterrupt):
            state.save()
            raise
        except Exception as e:  # noqa
            state.set(vid, note=f"whisper hata: {str(e)[:150]}")
            log.error("   whisper hata: %s", str(e).splitlines()[-1][:200])
        state.save()


def write_index(ctx, cdir):
    rows = []
    for vid, e in ctx.state.v.items():
        d = e.get("dir")
        ch = Path(d).parts[0] if d else e.get("channel")
        if ch == cdir.name:
            rows.append({"id": vid, "title": e.get("title"), "status": e.get("status"),
                         "source": e.get("source"), "folder": Path(d).name if d else None})
    if rows:
        atomic_write(cdir / "_index.json", json.dumps(rows, ensure_ascii=False, indent=2))


def write_summary(ctx):
    by = {}
    for vid, e in ctx.state.v.items():
        ch = e.get("channel") or "?"
        by.setdefault(ch, {}).setdefault(e.get("status", "?"), []).append((vid, e.get("title") or vid))
    lines = [f"# Ozet ({datetime.now():%Y-%m-%d %H:%M})", ""]
    for ch in sorted(by):
        c = by[ch]
        lines.append(f"## {ch}")
        lines.append(", ".join(f"{STATUS_TR.get(s, s)}: {len(v)}" for s, v in sorted(c.items())))
        for s in ("members_only", "private", "unavailable", "age_restricted", "no_subs", "no_formats", "error"):
            for vid, title in c.get(s, []):
                lines.append(f"- [{STATUS_TR[s]}] {title} ({vid})")
        lines.append("")
    atomic_write(ctx.root / "_summary.md", "\n".join(lines))


def print_status(state):
    total = {}
    per = {}
    for vid, e in state.v.items():
        st = e.get("status", "?")
        total[st] = total.get(st, 0) + 1
        per.setdefault(e.get("channel") or "?", {}).setdefault(st, 0)
        per[e.get("channel") or "?"][st] += 1
    print(f"Hafizada {len(state.v)} video: " + ", ".join(f"{STATUS_TR.get(k, k)}={v}" for k, v in sorted(total.items())))
    for ch, c in sorted(per.items()):
        print(f"  {ch}: " + ", ".join(f"{STATUS_TR.get(k, k)}={v}" for k, v in sorted(c.items())))


def run_diagnose(ctx, video):
    vid = extract_video_id(video) or "dQw4w9WgXcQ"
    args, out = ctx.args, []

    def say(t=""):
        print(t)
        out.append(t)
    say(f"=== TESHIS {datetime.now():%Y-%m-%d %H:%M:%S} | video {vid} ===")
    say(f"yt-dlp {yt_dlp.version.__version__} | python {sys.version.split()[0]}")
    js = [x for x in ("deno", "node", "bun", "qjs") if shutil.which(x)]
    say(f"JS calistirici: {', '.join(js) or 'YOK'} | cerez: {'evet' if ctx.cookies else 'hayir'} | "
        f"faster-whisper: {'var' if whisper_available() else 'yok'}")
    ip = probe(ctx.y, "https://api.ipify.org")
    say(f"Cikis IP: {ip['body'].strip()[:60] if ip['ok'] else 'ogrenilemedi (' + str(ip['error']) + ')'}")
    variants = [("CEREZSIZ", False)] + ([("CEREZLI", True)] if ctx.cookies else [])
    res = {}
    for label, use in variants:
        o = base_opts(args)
        if not use:
            o.pop("cookiefile", None)
            o.pop("cookiesfrombrowser", None)
        with YoutubeDL(o) as y:
            ok, lines = health_report(y, args, vid)
        say(f"\n--- {label}: {'SAGLIKLI' if ok else 'SORUNLU'} ---")
        for l in lines:
            say("  " + l)
        res[label] = ok
    if len(res) == 2 and res["CEREZSIZ"] != res["CEREZLI"]:
        say("\nNOT: cerezli ve cerezsiz sonuc FARKLI -> " + ("cerezsiz calisiyor, cerez soruna yol aciyor."
                                                           if res["CEREZSIZ"] else "cerezli calisiyor, cerezsiz engelli."))
    fp = ctx.root / f"_diagnose_{datetime.now():%Y%m%d_%H%M%S}.txt"
    atomic_write(fp, "\n".join(out) + "\n")
    say(f"\nRapor: {fp}")


def main():
    ap = argparse.ArgumentParser(description="Akilli YouTube transkript indirici")
    ap.add_argument("-t", "--channel", nargs="+", metavar="KANAL")
    ap.add_argument("-s", "--single", nargs="+", metavar="VIDEO")
    ap.add_argument("-f", "--file", nargs="+", metavar="TXT", help="kanal/video listesi dosyasi (satir satir)")
    ap.add_argument("-o", "--out", help="cikti klasoru (varsayilan: Desktop/transcripts)")
    ap.add_argument("-l", "--lang", default="en")
    ap.add_argument("--limit", type=int, help="her kanalda en fazla N video")
    ap.add_argument("--order", choices=["newest", "oldest"], default="newest")
    ap.add_argument("--force", action="store_true", help="biten videolari da yeniden indir")
    ap.add_argument("--retry-failed", action="store_true", help="uyelere-ozel/altyazisiz dahil hepsini tekrar dene")
    ap.add_argument("--refresh", action="store_true", help="kanal listesi onbellegini yoksay")
    ap.add_argument("--dry-run", action="store_true", help="indirmeden plani goster")
    ap.add_argument("--status", action="store_true", help="hafizadaki durumu goster (ag kullanmaz)")
    ap.add_argument("--manual-only", action="store_true")
    ap.add_argument("--whisper", choices=["off", "fallback", "always"], default="fallback")
    ap.add_argument("--whisper-model", default="large-v3")
    ap.add_argument("--no-streams", action="store_true")
    ap.add_argument("--shorts", action="store_true")
    ap.add_argument("--sleep-min", type=float, default=6.0)
    ap.add_argument("--sleep-max", type=float, default=14.0)
    ap.add_argument("--block-wait", type=int, default=30, help="429'da ilk bekleme (dk)")
    ap.add_argument("--block-max", type=int, default=90, help="tek bekleme ust siniri (dk)")
    ap.add_argument("--max-blocks", type=int, default=10)
    ap.add_argument("--net-wait", type=int, default=30, help="internet kopunca en fazla bekleme (dk)")
    ap.add_argument("--list-cache-hours", type=float, default=6.0)
    ap.add_argument("--no-health", action="store_true", help="baslangic saglik kontrolunu atla")
    ap.add_argument("--health-video", default="dQw4w9WgXcQ")
    ap.add_argument("--cookies")
    ap.add_argument("--cookies-from-browser")
    ap.add_argument("--diagnose", nargs="?", const="dQw4w9WgXcQ", metavar="VIDEO")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    root = Path(args.out).expanduser() if args.out else desktop_dir() / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger().handlers[0].setLevel(logging.DEBUG if args.verbose else logging.INFO)
    fh = logging.FileHandler(root / "transkript2.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)
    log.setLevel(logging.DEBUG)
    ERR_LOG["path"] = root / "_errors.jsonl"

    ctx = Ctx()
    ctx.args, ctx.root = args, root
    ctx.cookies = bool(args.cookies or args.cookies_from_browser)
    ctx.state = State(root)
    ctx.pacer, ctx.block_level, ctx.times = Pacer(args), 0, deque(maxlen=30)
    ctx.seen_ids, ctx.empty_seen = set(), set()

    if args.status:
        ctx.state.scan_existing()
        print_status(ctx.state)
        return
    targets = load_targets(args)
    if args.diagnose is None and not targets:
        ap.error("-f DOSYA, -t KANAL, -s VIDEO, --status veya --diagnose ver")

    if not args.dry_run and args.diagnose is None:
        acquire_lock(root)
    ctx.y = YoutubeDL(base_opts(args))

    ver = yt_dlp.version.__version__
    log.info("yt-dlp %s | cikti: %s", ver, root)
    try:
        age = (datetime.now() - datetime.strptime(ver[:10], "%Y.%m.%d")).days
        if age > 45:
            log.warning("UYARI: yt-dlp %d gun eski. Guncelle: python3 -m pip install -U yt-dlp", age)
    except Exception:  # noqa
        pass
    if not any(shutil.which(x) for x in ("deno", "node", "bun", "qjs")):
        log.warning("UYARI: JS calistirici (deno) yok -> bazi videolarda format/altyazi gelmeyebilir. "
                    "Kur: curl -fsSL https://deno.land/install.sh | sh")
    if args.whisper != "off" and not whisper_available():
        log.info("Not: faster-whisper kurulu degil; altyazisiz videolar hafizaya yazilir, sonra whisper ile islenebilir.")

    if args.diagnose is not None:
        run_diagnose(ctx, args.diagnose)
        return

    ctx.state.scan_existing()
    stats = {"ok": 0, "no_subs": 0, "skipped": 0, "failed": 0}
    try:
        if not args.no_health and not args.dry_run:
            ok, lines = health_report(ctx.y, args, args.health_video)
            for l in lines:
                log.info("saglik: %s", l)
            if not ok:
                log.warning("Baslamadan once engel var (yukaridaki GERCEK olcume bak). Kalkana kadar beklenecek.")
                ctx.block_level = 1
                if not cooldown(ctx, Blocked("baslangic saglik kontrolu basarisiz", "STARTUP")):
                    raise Abort()
                ctx.block_level = 0
            else:
                log.info("saglik: engel yok, basliyoruz.")

        n = len(targets)
        loose = []
        for idx, t in enumerate(targets, 1):
            if t["kind"] == "channel":
                try:
                    s = process_channel(ctx, t, idx, n)
                except Skip as e:
                    log.error("Kanal okunamadi (%s): %s\nIpucu: tam URL ver (https://www.youtube.com/@handle).", t["value"], e)
                    continue
                except Abort:
                    raise
                except Exception as e:  # noqa
                    log.error("Kanal okunamadi (%s): %s\nIpucu: tam URL ver (https://www.youtube.com/@handle) "
                              "ve 'pip install -U yt-dlp'.", t["value"], str(e).splitlines()[-1][:200])
                    continue
                for k in stats:
                    stats[k] += s[k]
            elif t["kind"] == "playlist":
                name, items = guarded(ctx, lambda: list_playlist(ctx, t["value"]))
                log.info("\n=== [%d/%d] playlist: %s ===", idx, n, name)
                s = process_loose(ctx, items, f"{idx}/{n}")
                for k in stats:
                    stats[k] += s[k]
            else:
                loose.append({"id": t["value"], "title": t["value"]})
        if loose:
            log.info("\n=== Tek videolar (%d) ===", len(loose))
            s = process_loose(ctx, loose, "tek")
            for k in stats:
                stats[k] += s[k]
        whisper_pass(ctx)
    except Abort:
        log.error("Calisma durduruldu (engel/ag). Tekrar calistirirsan kaldigi yerden devam eder.")
    except KeyboardInterrupt:
        log.warning("\nDurduruldu. Hafiza kaydedildi; tekrar calistirirsan kaldigi yerden devam eder.")
        ctx.state.save()
        write_summary(ctx)
        sys.exit(130)
    ctx.state.save()
    write_summary(ctx)
    log.info("\nBITTI -> yeni: %d | altyazisiz: %d | atlanan: %d | hatali: %d", stats["ok"], stats["no_subs"],
             stats["skipped"], stats["failed"])
    print_status(ctx.state)
    log.info("Ozet: %s | Hatalar: %s", root / "_summary.md", root / "_errors.jsonl")


if __name__ == "__main__":
    main()

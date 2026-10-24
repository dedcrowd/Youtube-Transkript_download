#!/usr/bin/env python3
"""
yt_transcripts.py - YouTube kanal / video transkript indirici

Kullanim:
    python yt_transcripts.py -t nahamsec
    python yt_transcripts.py -t nahamsec criticalthinkingpodcast
    python yt_transcripts.py -t https://www.youtube.com/@nahamsec --limit 5
    python yt_transcripts.py -s https://www.youtube.com/watch?v=XXXXXXXXXXX
    python yt_transcripts.py -t nahamsec --whisper always      (en kaliteli, yavas)

Cikti:
    ~/Desktop/transcripts/<Kanal Adi>/<Video Basligi [id]>/
        transcript.txt              duz metin (paragraflar)
        transcript_timestamped.txt  zaman damgali paragraflar
        transcript.srt              altyazi
        chunks.md                   ~10 dk'lik bloklar (LLM isleme icin)
        metadata.json               baslik, tarih, sure, bolumler, kaynak...
        description.txt             video aciklamasi
    ~/Desktop/transcripts/<Kanal Adi>/_index.json, _failed.txt, run.log

Kurulum:
    pip install -U yt-dlp
    pip install faster-whisper      (opsiyonel, altyazisiz/kotu videolar icin)
"""

import argparse
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
from datetime import datetime
from pathlib import Path

try:
    import yt_dlp
    from yt_dlp import YoutubeDL
except ImportError:
    sys.exit("yt-dlp kurulu degil. Kur: python -m pip install -U yt-dlp")

log = logging.getLogger("ytt")

BUG_BOUNTY_PROMPT = (
    "Bug bounty hacking talk. Terms: SSRF, IDOR, XSS, CSRF, SQLi, RCE, XXE, SSTI, "
    "OAuth, JWT, GraphQL, Burp Suite, ffuf, nuclei, httpx, subfinder, amass, "
    "HackerOne, Bugcrowd, Intigriti, recon, WAF bypass, CORS, subdomain takeover, "
    "NahamSec, Critical Thinking podcast, Justin Gardner, Rhynorater, Joel Margolis."
)

PERMANENT_ERRORS = (
    "private video", "members-only", "members only", "join this channel",
    "video unavailable", "has been removed", "this video is not available",
    "live event will begin", "premieres in", "terminated", "no longer available",
    "who has blocked it", "copyright",
)
BLOCK_MARKERS = ("429", "too many requests", "sign in to confirm", "not a bot")


class Skip(Exception):
    """Tekrar denenmesi anlamsiz hata."""


class Blocked(Exception):
    """YouTube istekleri engelliyor."""


class QuietLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        log.debug("yt-dlp: %s", msg)


# --------------------------------------------------------------------------
# Yardimcilar
# --------------------------------------------------------------------------
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}


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
    cands = [home / "Desktop", home / "OneDrive" / "Desktop", home / "Masaüstü"]
    for c in cands:
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


def extract_video_id(s):
    s = s.strip()
    if re.fullmatch(r"[\w-]{11}", s):
        return s
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})", s)
    return m.group(1) if m else None


def channel_base_url(t):
    t = t.strip()
    if t.startswith("http"):
        t = re.sub(r"/(videos|streams|shorts|featured|playlists|podcasts|live|about)/?$",
                   "", t.rstrip("/"))
        return t
    handle = t.lstrip("@").replace(" ", "")
    if re.fullmatch(r"UC[\w-]{22}", handle):
        return f"https://www.youtube.com/channel/{handle}"
    return f"https://www.youtube.com/@{handle}"


# --------------------------------------------------------------------------
# yt-dlp sarmalayicilari
# --------------------------------------------------------------------------
def base_opts(args):
    o = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "logger": QuietLogger(), "retries": 5, "fragment_retries": 5,
        "socket_timeout": 30, "ignoreerrors": False,
    }
    if args.cookies:
        o["cookiefile"] = args.cookies
    if args.cookies_from_browser:
        o["cookiesfrombrowser"] = (args.cookies_from_browser,)
    return o


def with_retries(fn, what, max_tries=4):
    for attempt in range(1, max_tries + 1):
        try:
            return fn()
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa
            msg = str(e)
            low = msg.lower()
            if any(p in low for p in PERMANENT_ERRORS):
                raise Skip(msg.splitlines()[-1][:200])
            if "confirm your age" in low or "age-restricted" in low:
                raise Skip("yas kisitlamali (--cookies-from-browser chrome dene)")
            blocked = any(b in low for b in BLOCK_MARKERS)
            if attempt == max_tries:
                if blocked:
                    raise Blocked(msg.splitlines()[-1][:200])
                raise
            wait = (60 * attempt + random.uniform(0, 15)) if blocked else (4 * 2 ** attempt + random.uniform(0, 3))
            log.warning("%s hata (deneme %d/%d): %s -> %.0fs bekleniyor",
                        what, attempt, max_tries, msg.splitlines()[-1][:120], wait)
            time.sleep(wait)


def flatten_entries(info):
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        if e.get("entries"):
            yield from flatten_entries(e)
        elif e.get("id"):
            yield e


def list_channel(target, args):
    """(kanal_adi, [ {id,title,tab}, ... ]) dondurur."""
    base = channel_base_url(target)
    tabs = ["videos"]
    if not args.no_streams:
        tabs.append("streams")
    if args.shorts:
        tabs.append("shorts")
    seen, items, name = set(), [], None
    for tab in tabs:
        url = f"{base}/{tab}"
        opts = {**base_opts(args), "extract_flat": "in_playlist", "skip_download": True}
        try:
            def go():
                with YoutubeDL(opts) as y:
                    return y.extract_info(url, download=False)
            info = with_retries(go, f"liste[{tab}]")
        except Skip as e:
            log.info("%s sekmesi atlandi: %s", tab, e)
            continue
        except Exception as e:  # noqa
            if tab == "videos":
                raise
            log.info("%s sekmesi yok/okunamadi: %s", tab, str(e).splitlines()[-1][:100])
            continue
        name = name or info.get("channel") or info.get("uploader") or info.get("title")
        n = 0
        for e in flatten_entries(info):
            if e["id"] in seen or not re.fullmatch(r"[\w-]{11}", e["id"]):
                continue
            seen.add(e["id"])
            items.append({"id": e["id"], "title": e.get("title") or e["id"], "tab": tab})
            n += 1
        log.info("%s: %d video bulundu", tab, n)
    name = re.sub(r"\s*-\s*(Videos|Live|Shorts)$", "", name or "", flags=re.I)
    return name or target.lstrip("@"), items


# --------------------------------------------------------------------------
# Altyazi isleme
# --------------------------------------------------------------------------
def parse_json3(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    segs = []
    for ev in data.get("events", []):
        if "segs" not in ev:
            continue
        text = "".join(s.get("utf8", "") for s in ev["segs"])
        text = html.unescape(text).replace("\n", " ")
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
    return "\n".join(
        f"{i}\n{fmt_ts(s['start'], True)} --> {fmt_ts(s['end'], True)}\n{s['text']}\n"
        for i, s in enumerate(segs, 1))


def build_chunks(segs, title, url, minutes=10):
    size = minutes * 60
    buckets = {}
    for s in segs:
        buckets.setdefault(int(s["start"] // size), []).append(s)
    lines = [f"# {title}", f"{url}", ""]
    for k in sorted(buckets):
        lines.append(f"## [{fmt_ts(k * size)} - {fmt_ts((k + 1) * size)}]")
        lines.append("")
        for t, para in paragraphs(buckets[k], max_span=60):
            lines.append(f"[{fmt_ts(t)}] {para}")
            lines.append("")
    return "\n".join(lines)


def looks_suspect(segs, duration):
    """Cok az kelime varsa altyazi eksik/bozuk olabilir."""
    if not duration or duration < 180:
        return False
    words = sum(len(s["text"].split()) for s in segs)
    return words / (duration / 60.0) < 25


# --------------------------------------------------------------------------
# Altyazi indirme / Whisper
# --------------------------------------------------------------------------
def pick_lang(avail, langs, auto):
    keys = list(avail.keys())
    for l in langs:
        if auto:
            for cand in (f"{l}-orig", l):
                if cand in avail:
                    return cand
        else:
            if l in avail:
                return l
    for l in langs:
        pref = l.split("-")[0]
        for k in keys:
            if k == pref or (k.startswith(pref + "-") and not k.endswith("-orig") and not auto):
                return k
    return None


def download_subs(url, info, tmp, args):
    langs = [l.strip() for l in args.lang.split(",") if l.strip()]
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    choices = []
    k = pick_lang(manual, langs, auto=False)
    if k:
        choices.append((k, False))
    if not args.manual_only:
        k = pick_lang(auto, langs, auto=True)
        if k:
            choices.append((k, True))
    for lang, is_auto in choices:
        opts = {**base_opts(args), "skip_download": True,
                "writesubtitles": not is_auto, "writeautomaticsub": is_auto,
                "subtitleslangs": [lang], "subtitlesformat": "json3",
                "outtmpl": str(tmp / "sub.%(ext)s")}

        def go():
            with YoutubeDL(opts) as y:
                y.download([url])
        with_retries(go, "altyazi")
        files = sorted(tmp.glob("sub*.json3"))
        if files:
            segs = parse_json3(files[0])
            for f in files:
                f.unlink(missing_ok=True)
            if segs:
                return segs, ("manual" if not is_auto else "auto"), lang
    return None


_whisper_model = None


def whisper_available():
    try:
        import faster_whisper  # noqa
        return True
    except ImportError:
        return False


def whisper_transcribe(url, tmp, args, lang_hint):
    global _whisper_model
    from faster_whisper import WhisperModel
    opts = {**base_opts(args), "format": "bestaudio/best",
            "outtmpl": str(tmp / "audio.%(ext)s")}

    def go():
        with YoutubeDL(opts) as y:
            y.download([url])
    with_retries(go, "ses indirme")
    audio = next(iter(sorted(tmp.glob("audio.*"))), None)
    if not audio:
        raise RuntimeError("ses dosyasi indirilemedi")
    if _whisper_model is None:
        log.info("Whisper modeli yukleniyor (%s)...", args.whisper_model)
        _whisper_model = WhisperModel(args.whisper_model, device="auto", compute_type="auto")
    lang = (lang_hint or args.lang.split(",")[0]).split("-")[0] or None
    it, _ = _whisper_model.transcribe(
        str(audio), language=lang, vad_filter=True, beam_size=5,
        initial_prompt=BUG_BOUNTY_PROMPT, condition_on_previous_text=False)
    segs = []
    for s in it:
        t = s.text.strip()
        if t:
            segs.append({"start": s.start, "end": s.end, "text": t})
    audio.unlink(missing_ok=True)
    return segs


# --------------------------------------------------------------------------
# Video isleme
# --------------------------------------------------------------------------
def find_existing(dirs, vid):
    for root in dirs:
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if d.is_dir() and d.name.endswith(f"[{vid}]") and (d / "metadata.json").exists():
                return d
    return None


def process_video(vid, channel_dir, root, args):
    url = f"https://www.youtube.com/watch?v={vid}"
    search_dirs = [channel_dir] if channel_dir else [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
    if not args.force:
        ex = find_existing(search_dirs, vid)
        if ex:
            return "skipped", ex, None

    opts = {**base_opts(args), "skip_download": True}

    def get_info():
        with YoutubeDL(opts) as y:
            return y.extract_info(url, download=False)
    info = with_retries(get_info, "video bilgisi")

    if info.get("live_status") in ("is_live", "is_upcoming"):
        raise Skip("canli yayin / premiere henuz bitmedi")

    title = info.get("title") or vid
    if channel_dir is None:
        ch = sanitize(info.get("channel") or info.get("uploader") or "unknown_channel")
        channel_dir = root / ch
    channel_dir.mkdir(parents=True, exist_ok=True)
    out_dir = channel_dir / f"{sanitize(title, 90)} [{vid}]"
    duration = info.get("duration") or 0

    tmp = Path(tempfile.mkdtemp(prefix="ytt_"))
    try:
        result = None
        if args.whisper != "always":
            result = download_subs(url, info, tmp, args)
            if result and args.whisper == "fallback" and result[1] == "auto" \
                    and looks_suspect(result[0], duration) and whisper_available():
                log.info("Altyazi supheli gorunuyor, Whisper'a geciliyor")
                result = None
        if result is None and args.whisper != "off":
            if whisper_available():
                lang_hint = info.get("language")
                segs = whisper_transcribe(url, tmp, args, lang_hint)
                if segs:
                    result = (segs, "whisper", lang_hint or args.lang)
            else:
                log.warning("Whisper kurulu degil (pip install faster-whisper)")
        if not result or not result[0]:
            raise Skip("transkript bulunamadi (altyazi yok, whisper kapali/kurulu degil)")
        segs, source, lang = result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    paras = paragraphs(segs)
    header = f"{title}\n{url}\n\n"
    atomic_write(out_dir / "transcript.txt", header + "\n\n".join(p for _, p in paras) + "\n")
    atomic_write(out_dir / "transcript_timestamped.txt",
                 header + "\n\n".join(f"[{fmt_ts(t)}] {p}" for t, p in paras) + "\n")
    atomic_write(out_dir / "transcript.srt", build_srt(segs))
    atomic_write(out_dir / "chunks.md", build_chunks(segs, title, url))
    if info.get("description"):
        atomic_write(out_dir / "description.txt", info["description"])
    meta = {
        "id": vid, "title": title, "url": url,
        "channel": info.get("channel") or info.get("uploader"),
        "upload_date": info.get("upload_date"), "duration_sec": duration,
        "view_count": info.get("view_count"), "was_live": info.get("was_live"),
        "tags": info.get("tags"), "chapters": info.get("chapters"),
        "transcript_source": source, "language": lang,
        "segments": len(segs), "words": sum(len(s["text"].split()) for s in segs),
        "downloaded_at": datetime.now().isoformat(timespec="seconds"), "status": "ok",
    }
    atomic_write(out_dir / "metadata.json", json.dumps(meta, ensure_ascii=False, indent=2))
    return "ok", out_dir, meta


# --------------------------------------------------------------------------
# Calistirma
# --------------------------------------------------------------------------
def update_index(channel_dir, entry):
    p = channel_dir / "_index.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa
        data = {}
    data[entry["id"]] = entry
    atomic_write(p, json.dumps(data, ensure_ascii=False, indent=2))


def add_file_log(channel_dir):
    h = logging.FileHandler(channel_dir / "run.log", encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    return h


def run_batch(items, channel_dir, root, args):
    stats = {"ok": 0, "skipped": 0, "failed": 0}
    blocks = 0
    total = len(items)
    for i, it in enumerate(items, 1):
        vid, title = it["id"], it.get("title", it["id"])
        log.info("[%d/%d] %s (%s)", i, total, title[:80], vid)
        networked = True
        try:
            status, out, meta = process_video(vid, channel_dir, root, args)
            blocks = 0
            if status == "skipped":
                stats["skipped"] += 1
                networked = False
                log.info("   zaten var, atlandi")
            else:
                stats["ok"] += 1
                log.info("   OK [%s] %d kelime -> %s", meta["transcript_source"], meta["words"], out.name)
            cdir = out.parent
            if status == "ok":
                update_index(cdir, {"id": vid, "title": meta["title"], "folder": out.name,
                                    "source": meta["transcript_source"], "status": "ok",
                                    "upload_date": meta["upload_date"]})
        except Skip as e:
            stats["failed"] += 1
            log.warning("   atlandi: %s", e)
            record_failure(channel_dir, vid, title, str(e))
        except Blocked as e:
            blocks += 1
            stats["failed"] += 1
            log.error("   YouTube engelledi: %s", e)
            record_failure(channel_dir, vid, title, f"blocked: {e}")
            if blocks >= 3:
                log.error("Ust uste engel alindi. Cozum: biraz bekle, VPN/IP degistir veya "
                          "--cookies-from-browser chrome kullan. Tekrar calistirirsan kaldigi yerden devam eder.")
                break
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa
            stats["failed"] += 1
            log.error("   hata: %s", str(e).splitlines()[-1][:200])
            log.debug("detay", exc_info=True)
            record_failure(channel_dir, vid, title, str(e).splitlines()[-1][:200])
        if networked and i < total:
            time.sleep(random.uniform(args.sleep_min, args.sleep_max))
    return stats


def record_failure(channel_dir, vid, title, reason):
    d = channel_dir if channel_dir else None
    if d is None:
        return
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "_failed.txt", "a", encoding="utf-8") as f:
        f.write(f"{vid}\t{title}\t{reason}\n")


def main():
    ap = argparse.ArgumentParser(description="YouTube kanal/video transkript indirici")
    ap.add_argument("-t", "--channel", nargs="+", metavar="KANAL",
                    help="kanal adi/@handle/URL (birden fazla verilebilir)")
    ap.add_argument("-s", "--single", nargs="+", metavar="VIDEO",
                    help="tek video URL veya ID (birden fazla verilebilir)")
    ap.add_argument("-o", "--out", help="cikti klasoru (varsayilan: Desktop/transcripts)")
    ap.add_argument("-l", "--lang", default="en", help="dil onceligi, virgullu (varsayilan: en)")
    ap.add_argument("--limit", type=int, help="kanalda en fazla N video")
    ap.add_argument("--force", action="store_true", help="var olanlari yeniden indir")
    ap.add_argument("--manual-only", action="store_true", help="sadece insan yazimi altyazi")
    ap.add_argument("--whisper", choices=["off", "fallback", "always"], default="fallback",
                    help="fallback: altyazi yok/bozuksa Whisper (varsayilan)")
    ap.add_argument("--whisper-model", default="large-v3")
    ap.add_argument("--no-streams", action="store_true", help="canli yayin kayitlarini atla")
    ap.add_argument("--shorts", action="store_true", help="shorts'lari da dahil et")
    ap.add_argument("--sleep-min", type=float, default=3.0)
    ap.add_argument("--sleep-max", type=float, default=8.0)
    ap.add_argument("--cookies", help="cookies.txt yolu")
    ap.add_argument("--cookies-from-browser", help="chrome, firefox, edge...")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not args.channel and not args.single:
        ap.error("-t KANAL veya -s VIDEO ver")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger().handlers[0].setLevel(logging.INFO)
    log.setLevel(logging.DEBUG)

    root = Path(args.out).expanduser() if args.out else desktop_dir() / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    log.info("yt-dlp %s | cikti: %s", yt_dlp.version.__version__, root)
    if args.whisper != "off" and not whisper_available():
        log.info("Not: faster-whisper kurulu degil, sadece YouTube altyazilari kullanilacak.")

    total = {"ok": 0, "skipped": 0, "failed": 0}
    try:
        for target in args.channel or []:
            log.info("\n=== Kanal: %s ===", target)
            try:
                name, items = list_channel(target, args)
            except Exception as e:  # noqa
                log.error("Kanal okunamadi: %s\nIpucu: tam URL ver (https://www.youtube.com/@handle) "
                          "ve 'pip install -U yt-dlp' ile guncelle.", str(e).splitlines()[-1][:200])
                continue
            if args.limit:
                items = items[:args.limit]
            cdir = root / sanitize(name)
            cdir.mkdir(parents=True, exist_ok=True)
            fh = add_file_log(cdir)
            log.info("Kanal: %s | %d video | klasor: %s", name, len(items), cdir)
            st = run_batch(items, cdir, root, args)
            log.removeHandler(fh)
            fh.close()
            for k in total:
                total[k] += st[k]
            log.info("Kanal ozeti: %s", st)

        if args.single:
            log.info("\n=== Tek video ===")
            items = []
            for s in args.single:
                vid = extract_video_id(s)
                if not vid:
                    log.error("Gecersiz video: %s", s)
                    continue
                items.append({"id": vid, "title": s})
            st = run_batch(items, None, root, args)
            for k in total:
                total[k] += st[k]
    except KeyboardInterrupt:
        log.warning("\nDurduruldu. Tekrar calistirirsan kaldigi yerden devam eder.")
        sys.exit(130)

    log.info("\nBitti -> basarili: %d | atlanan(var): %d | basarisiz: %d",
             total["ok"], total["skipped"], total["failed"])
    log.info("Klasor: %s", root)


if __name__ == "__main__":
    main()

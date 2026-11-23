"""Serve a local multi-device ambient photo frame, gallery browser, and pocket remote.

Zero required third-party dependencies (Python stdlib `ThreadingHTTPServer`, with optional
`numpy` acceleration for cosine similarity over `album_embeddings.npz`).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import threading
import urllib.error
from urllib.parse import parse_qs, quote, unquote, urlsplit
import urllib.request

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
DEFAULT_CURATED_DIR = Path.cwd() / "data" / "album"
PROFILE_PATH = Path.cwd() / "private" / "profile.json"
_PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8")) if PROFILE_PATH.is_file() else {}
DEFAULT_GATEWAY = os.environ.get("OPENAI_BASE_URL") or _PROFILE.get("openai_base_url") or "https://api.openai.com/v1"
GATEWAY_TOKEN = os.environ.get("OPENAI_API_KEY") or _PROFILE.get("openai_api_key") or ""

IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/gallery": ("gallery.html", "text/html; charset=utf-8"),
    "/remote": ("remote.html", "text/html; charset=utf-8"),
    "/album.js": ("album.js", "text/javascript; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/album.css": ("album.css", "text/css; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/gallery.js": ("gallery.js", "text/javascript; charset=utf-8"),
    "/remote.js": ("remote.js", "text/javascript; charset=utf-8"),
    "/silence.wav": ("silence.wav", "audio/wav"),
    "/fonts/InstrumentSerif-Regular.ttf": ("fonts/InstrumentSerif-Regular.ttf", "font/ttf"),
    "/fonts/InstrumentSerif-Italic.ttf": ("fonts/InstrumentSerif-Italic.ttf", "font/ttf"),
    "/fonts/Manrope-Variable.ttf": ("fonts/Manrope-Variable.ttf", "font/ttf"),
}

ALLOWED_INTERVALS = (10, 15, 30, 60, 300)


def classify_device_label(ua: str, role: str = "") -> str:
    low = (ua or "").lower()
    if "silk" in low and ("a1nl4bvlq4l3n3" in low or "echo" in low or "aft" in low):
        return "Echo Show (Silk)"
    if "silk" in low:
        return "Amazon Silk Display"
    if "ipad" in low:
        return "iPad"
    if "iphone" in low:
        return "iPhone"
    if "macintosh" in low or "mac os x" in low:
        return "Mac"
    if "android" in low:
        return "Android Display"
    return "Display Frame" if role == "display" else "LAN Viewer"


def day_of_year_distance(mm_dd: str, ref: date) -> int:
    if not mm_dd or len(mm_dd) != 5 or mm_dd[2] != "-":
        return 999
    try:
        m, d = int(mm_dd[:2]), int(mm_dd[3:5])
        cand = date(2024, m, min(d, 28 if m == 2 else d))
        ref_leap = date(2024, ref.month, min(ref.day, 28 if ref.month == 2 else ref.day))
        diff = abs((cand - ref_leap).days)
        return min(diff, 366 - diff)
    except ValueError:
        return 999


def lexical_score(query: str, slide: dict) -> float:
    tokens = [t for t in re.split(r"[^a-z0-9]+", query.lower()) if len(t) >= 2]
    if not tokens:
        return 0.0
    haystack = " ".join(
        [
            str(slide.get("display_caption") or ""),
            str(slide.get("search_detail") or ""),
            str(slide.get("visual_theme") or "").replace("_", " "),
            str(slide.get("color_palette") or "").replace("_", " "),
            str(slide.get("date_label") or ""),
            str(slide.get("age_badge") or ""),
            str(slide.get("year") or ""),
            " ".join(slide.get("channels") or []),
            " ".join(slide.get("tags") or []),
        ]
    ).lower()
    hits = sum(1 for tok in tokens if tok in haystack)
    return hits / float(len(tokens))


def load_hidden_ids(album_dir: Path) -> set[str]:
    hidden_path = album_dir / "hidden.json"
    if not hidden_path.is_file():
        return set()
    try:
        raw = json.loads(hidden_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(x) for x in raw} if isinstance(raw, list) else set()


def is_slide_hidden(slide: dict, hidden_ids: set[str]) -> bool:
    if slide.get("id") in hidden_ids:
        return True
    if slide.get("photo_id") and slide["photo_id"] in hidden_ids:
        return True
    for mid in slide.get("member_ids") or []:
        if mid in hidden_ids:
            return True
    return False


class AlbumServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, photos: Path | None = None, gateway_url: str = DEFAULT_GATEWAY):
        self._photos: Path | None = None
        self.gateway_url = gateway_url.rstrip("/")
        self.clients: dict[str, dict] = {}
        self.lock = threading.Lock()

        # Live remote-controlled display state
        self.revision = 1
        self.command_seq = 0
        self.last_command: dict = {"seq": 0, "action": "none", "slide_id": None}
        self.active_channel = "smart_frame"
        self.active_query = ""
        self.search_slide_ids: list[str] = []
        self.interval_sec = 10
        self.paused = False
        self.show_captions = True
        self.show_clock = True

        # Cached curated manifest & semantic embeddings
        self._manifest_mtime: float = -1.0
        self._manifest: dict | None = None
        self._embed_ids: list[str] = []
        self._embed_matrix = None
        self._query_vec_cache: dict[str, list[float]] = {}
        self._hidden_ids: set[str] = set()

        self.photos = photos
        super().__init__(address, Handler)

    @property
    def photos(self) -> Path | None:
        return self._photos

    @photos.setter
    def photos(self, value: Path | None) -> None:
        self._photos = value.resolve() if value else None
        self._manifest_mtime = -1.0
        self._manifest = None
        self._embed_ids = []
        self._embed_matrix = None
        self._hidden_ids = set()
        if self._photos:
            self._maybe_reload_curated()

    def _hidden_file(self) -> Path | None:
        if not self._photos:
            return None
        return self._photos / "hidden.json"

    def _load_hidden(self) -> None:
        self._hidden_ids = load_hidden_ids(self._photos) if self._photos else set()

    def _save_hidden(self) -> None:
        hf = self._hidden_file()
        if not hf or not self._photos or not (self._photos / "manifest.json").is_file():
            return
        hf.write_text(json.dumps(sorted(self._hidden_ids), indent=2), encoding="utf-8")

    def _maybe_reload_curated(self) -> dict | None:
        if not self._photos:
            self._manifest = None
            return None
        mpath = self._photos / "manifest.json"
        if not mpath.is_file():
            self._manifest = None
            return None
        try:
            mtime = mpath.stat().st_mtime
        except OSError:
            return self._manifest
        if self._manifest is not None and mtime == self._manifest_mtime:
            return self._manifest

        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            return self._manifest

        self._manifest = manifest
        self._manifest_mtime = mtime
        self._load_hidden()

        epath = self._photos / "album_embeddings.npz"
        if epath.is_file() and np is not None:
            try:
                loaded = np.load(epath, allow_pickle=False)
                self._embed_ids = [str(x) for x in loaded["ids"].tolist()]
                self._embed_matrix = loaded["vectors"].astype(np.float32)
            except Exception:
                self._embed_ids = []
                self._embed_matrix = None
        return self._manifest

    def is_curated(self) -> bool:
        return self._maybe_reload_curated() is not None

    def images(self) -> list[Path]:
        if not self._photos:
            return []
        return sorted(
            p
            for p in self._photos.iterdir()
            if p.is_file() and not p.is_symlink() and p.suffix.lower() in IMAGE_TYPES
        )

    def _is_slide_hidden(self, slide: dict) -> bool:
        return is_slide_hidden(slide, self._hidden_ids)

    def _plain_folder_slides(self) -> list[dict]:
        imgs = self.images()
        out = []
        for p in imgs:
            sid = p.name
            if sid in self._hidden_ids:
                continue
            out.append(
                {
                    "id": sid,
                    "kind": "single",
                    "src": "/photos/" + quote(p.name, safe=""),
                    "label": p.stem,
                    "display_caption": p.stem,
                    "date_label": "",
                    "age_badge": "",
                    "accent_rgb": [28, 38, 52],
                    "orientation": "landscape",
                    "aspect_ratio": 1.5,
                    "channels": ["smart_frame", "featured"],
                }
            )
        return out

    def _slides_for_channel(self, channel_id: str, include_hidden: bool = False) -> list[dict]:
        manifest = self._maybe_reload_curated()
        if manifest is None:
            return self._plain_folder_slides()

        all_slides: list[dict] = manifest.get("slides") or []
        visible = [s for s in all_slides if include_hidden or not self._is_slide_hidden(s)]

        if channel_id == "all":
            return visible
        if channel_id == "smart_frame":
            res = [s for s in visible if s.get("in_default_frame", True)]
            return res or visible
        if channel_id == "on_this_day":
            today = datetime.now().date()
            near = [s for s in visible if day_of_year_distance(s.get("month_day", ""), today) <= 24]
            if len(near) >= 4:
                return near
            # Graceful seasonal fallback so channel is always rich
            cur_month = f"-{today.month:02d}-"
            seasonal = [s for s in visible if cur_month in f"-{s.get('month_day', '')}-"]
            return seasonal or visible[:24]
        if channel_id == "search" and self.search_slide_ids:
            by_id = {s["id"]: s for s in visible}
            matched = [by_id[sid] for sid in self.search_slide_ids if sid in by_id]
            return matched or visible[:20]

        filtered = [s for s in visible if channel_id in (s.get("channels") or [])]
        return filtered or visible

    def _format_frame_slide(self, s: dict) -> dict:
        badge = s.get("age_badge") or ""
        return {
            "id": s["id"],
            "src": s["src"],
            "label": s.get("display_caption") or s.get("label") or s["id"],
            "display_caption": s.get("display_caption") or s.get("label") or "",
            "date_label": s.get("date_label") or "",
            "age_badge": badge,
            "accent_rgb": s.get("accent_rgb") or [24, 32, 44],
            "kind": s.get("kind") or "single",
            "orientation": s.get("orientation") or "landscape",
            "aspect_ratio": s.get("aspect_ratio") or 1.5,
            "frame_score": s.get("frame_score") or 8.0,
            "year": s.get("year") or "",
            "channels": s.get("channels") or [],
            "resolution_tier": s.get("resolution_tier") or "full_2048",
        }

    def build_state_payload(self) -> dict:
        manifest = self._maybe_reload_curated()
        with self.lock:
            active_ch = self.active_channel
            active_q = self.active_query
            rev = self.revision
            cmd = dict(self.last_command)
            interval = self.interval_sec
            paused = self.paused
            show_captions = self.show_captions
            show_clock = self.show_clock

        slides = [self._format_frame_slide(s) for s in self._slides_for_channel(active_ch)]
        if manifest is not None:
            raw_channels = manifest.get("channels") or []
            channels_out = [
                {
                    "id": ch["id"],
                    "name": ch["name"],
                    "description": ch.get("description") or "",
                    "count": len(self._slides_for_channel(ch["id"])),
                }
                for ch in raw_channels
            ]
            if active_ch == "search" and active_q:
                channels_out.insert(
                    0,
                    {
                        "id": "search",
                        "name": f'Search: "{active_q}"',
                        "description": "Live OpenAI semantic search playlist",
                        "count": len(slides),
                    },
                )
        else:
            channels_out = [
                {
                    "id": "smart_frame",
                    "name": "Local Folder",
                    "description": "Photos in selected folder",
                    "count": len(slides),
                }
            ]

        return {
            "demo": len(slides) == 0,
            "curated": manifest is not None,
            "revision": rev,
            "command": cmd,
            "active_channel": active_ch,
            "active_query": active_q,
            "interval_sec": interval,
            "paused": paused,
            "show_captions": show_captions,
            "show_clock": show_clock,
            "hidden_count": len(self._hidden_ids),
            "channels": channels_out,
            "slides": slides,
        }

    def build_album_payload(self) -> dict:
        manifest = self._maybe_reload_curated()
        state = self.build_state_payload()
        if manifest is None:
            return {
                "curated": False,
                "stats": {"curated_photos": len(state["slides"]), "diptych_slides": 0},
                "hidden_ids": sorted(self._hidden_ids),
                "channels": state["channels"],
                "slides": state["slides"],
                "photos": state["slides"],
                "state": state,
            }

        all_slides = []
        for s in manifest.get("slides") or []:
            formatted = self._format_frame_slide(s)
            formatted["hidden"] = self._is_slide_hidden(s)
            formatted["in_default_frame"] = bool(s.get("in_default_frame", True))
            formatted["member_ids"] = s.get("member_ids") or ([s["photo_id"]] if s.get("photo_id") else [])
            formatted["visual_theme"] = s.get("visual_theme") or ""
            formatted["color_palette"] = s.get("color_palette") or ""
            formatted["tags"] = s.get("tags") or []
            all_slides.append(formatted)

        return {
            "curated": True,
            "stats": manifest.get("stats") or {},
            "hidden_ids": sorted(self._hidden_ids),
            "channels": state["channels"],
            "slides": all_slides,
            "photos": manifest.get("photos") or [],
            "state": state,
        }

    def _embed_query_live(self, query: str) -> list[float] | None:
        key = query.strip().lower()
        with self.lock:
            if key in self._query_vec_cache:
                return self._query_vec_cache[key]
        payload = json.dumps({"model": "text-embedding-3-large", "input": [query.strip()]}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.gateway_url}/embeddings",
            data=payload,
            headers={
                "Authorization": f"Bearer {GATEWAY_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                vec = data["data"][0]["embedding"]
                norm = math.sqrt(sum(x * x for x in vec)) or 1.0
                unit = [x / norm for x in vec]
                with self.lock:
                    if len(self._query_vec_cache) > 128:
                        self._query_vec_cache.clear()
                    self._query_vec_cache[key] = unit
                return unit
        except Exception:
            return None

    def semantic_search(self, query: str, top_k: int = 36) -> dict:
        q = (query or "").strip()
        manifest = self._maybe_reload_curated()
        if not q:
            slides = [self._format_frame_slide(s) for s in self._slides_for_channel("smart_frame")[:top_k]]
            return {"query": "", "mode": "default", "results": slides}

        candidates = (
            [s for s in (manifest.get("slides") or []) if not self._is_slide_hidden(s)]
            if manifest is not None
            else self._plain_folder_slides()
        )
        if not candidates:
            return {"query": q, "mode": "empty", "results": []}

        mode = "lexical_fallback"
        scored: list[tuple[float, dict]] = []

        q_vec = self._embed_query_live(q) if (manifest is not None and np is not None and self._embed_matrix is not None) else None
        if q_vec is not None and self._embed_matrix is not None and len(self._embed_ids) > 0:
            mode = "openai_embeddings"
            q_arr = np.asarray(q_vec, dtype=np.float32)
            cos_scores = self._embed_matrix @ q_arr
            cos_by_id = {sid: float(sc) for sid, sc in zip(self._embed_ids, cos_scores.tolist())}
            for s in candidates:
                cos = cos_by_id.get(s["id"], 0.0)
                lex = lexical_score(q, s)
                combined = cos * 0.85 + lex * 0.15
                scored.append((combined, s))
            scored.sort(key=lambda t: -t[0])
            top_score = scored[0][0] if scored else 0.0
            cutoff = max(0.20, top_score - 0.14)
            filtered = [(sc, s) for sc, s in scored if sc >= cutoff][:top_k]
            if len(filtered) < 4:
                filtered = scored[: min(12, len(scored))]
            scored = filtered
        else:
            for s in candidates:
                sc = lexical_score(q, s)
                if sc > 0:
                    scored.append((sc, s))
            scored.sort(key=lambda t: (-t[0], -(t[1].get("frame_score") or 0.0)))
            scored = scored[:top_k]

        results = []
        for sc, s in scored:
            item = self._format_frame_slide(s)
            item["score"] = round(float(sc), 4)
            results.append(item)
        return {"query": q, "mode": mode, "results": results}

    def resolve_media_file(self, subdir: str | None, raw_name: str) -> Path | None:
        if not self._photos or not raw_name:
            return None
        if Path(raw_name).name != raw_name:
            return None
        suffix = Path(raw_name).suffix.lower()
        if suffix not in IMAGE_TYPES:
            return None

        base_dir = (self._photos / subdir) if subdir else self._photos
        try:
            resolved_base = base_dir.resolve()
        except OSError:
            return None
        candidate = base_dir / raw_name
        if candidate.is_symlink() or not candidate.is_file():
            return None
        try:
            if candidate.resolve().parent != resolved_base:
                return None
        except OSError:
            return None
        return candidate


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Request URLs and search queries are personal; do not log them.
        pass

    def send(self, body: bytes, mime: str = "application/json", status: int = 200, cache_sec: int = 0) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        if cache_sec > 0:
            self.send_header("Cache-Control", f"public, max-age={cache_sec}")
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet, noimageindex")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, status: int = 200) -> None:
        self.send(json.dumps(value).encode("utf-8"), mime="application/json; charset=utf-8", status=status)

    def read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if length <= 0 or length > 65536:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _record_heartbeat(self, query_params: dict[str, list[str]]) -> None:
        ua = self.headers.get("User-Agent", "")[:512]
        role = (query_params.get("role", [""])[0] or "")[:24]
        slide_id = (query_params.get("slide", [""])[0] or "")[:64]
        caption = (query_params.get("caption", [""])[0] or "")[:120]
        ip = self.client_address[0]
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.server.lock:
            prev = self.server.clients.get(ip) or {}
            entry = {
                "ip": ip,
                "user_agent": ua,
                "device_name": classify_device_label(ua, role=role or prev.get("role", "")),
                "role": role or prev.get("role", "viewer"),
                "current_slide": slide_id or prev.get("current_slide", ""),
                "current_caption": caption or prev.get("current_caption", ""),
                "last_seen": now_iso,
            }
            self.server.clients[ip] = entry

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path = split.path
        params = parse_qs(split.query)

        if path == "/robots.txt":
            self.send(b"User-agent: *\nDisallow: /\n", mime="text/plain; charset=utf-8", cache_sec=3600)
        elif path == "/api/photos":
            if self.server.is_curated():
                state = self.server.build_state_payload()
                self.send_json({"demo": state["demo"], "photos": state["slides"]})
            else:
                images = self.server.images()
                self.send_json(
                    {
                        "demo": not bool(images),
                        "photos": [{"src": "/photos/" + quote(p.name, safe=""), "label": p.stem} for p in images],
                    }
                )
        elif path == "/api/state":
            if params.get("role") == ["display"]:
                self._record_heartbeat(params)
            self.send_json(self.server.build_state_payload())
        elif path == "/api/album":
            self.send_json(self.server.build_album_payload())
        elif path == "/api/search":
            q = (params.get("q", [""])[0] or "").strip()[:200]
            try:
                limit = min(max(int(params.get("limit", ["36"])[0]), 1), 120)
            except ValueError:
                limit = 36
            self.send_json(self.server.semantic_search(q, top_k=limit))
        elif path == "/api/status":
            state = self.server.build_state_payload()
            with self.server.lock:
                clients_list = list(self.server.clients.values())
            self.send_json(
                {
                    "clients": clients_list,
                    "active_channel": state["active_channel"],
                    "active_query": state["active_query"],
                    "interval_sec": state["interval_sec"],
                    "paused": state["paused"],
                    "slide_count": len(state["slides"]),
                }
            )
        elif path == "/api/ping":
            self._record_heartbeat(params)
            self.send_json({"ok": True})
        elif path in ASSETS:
            rel_name, mime = ASSETS[path]
            asset_file = WEB / rel_name
            if not asset_file.is_file():
                self.send_json({"error": "Not found"}, 404)
                return
            cache_ttl = 3600 if rel_name.startswith("fonts/") else 0
            self.send(asset_file.read_bytes(), mime, cache_sec=cache_ttl)
        elif path.startswith("/photos/") and self.server.photos:
            name = unquote(path[len("/photos/") :])
            # Support both curated (`data/album/photos/<name>`) and plain-folder (`photos_dir/<name>`)
            candidate = (
                self.server.resolve_media_file("photos", name)
                if self.server.is_curated()
                else self.server.resolve_media_file(None, name)
            )
            if candidate is None:
                self.send_json({"error": "Not found"}, 404)
                return
            self.send(candidate.read_bytes(), IMAGE_TYPES[candidate.suffix.lower()], cache_sec=300)
        elif path.startswith("/diptychs/") and self.server.photos and self.server.is_curated():
            name = unquote(path[len("/diptychs/") :])
            candidate = self.server.resolve_media_file("diptychs", name)
            if candidate is None:
                self.send_json({"error": "Not found"}, 404)
                return
            self.send(candidate.read_bytes(), IMAGE_TYPES[candidate.suffix.lower()], cache_sec=300)
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        body = self.read_json_body()
        if body is None:
            self.send_json({"error": "Invalid JSON body"}, 400)
            return

        if path == "/api/control":
            changed = False
            search_q = body.get("search_query")
            if isinstance(search_q, str) and search_q.strip():
                res = self.server.semantic_search(search_q.strip(), top_k=40)
                with self.server.lock:
                    self.server.active_query = search_q.strip()[:120]
                    self.server.search_slide_ids = [r["id"] for r in res["results"]]
                    self.server.active_channel = "search"
                    changed = True

            channel = body.get("channel")
            if isinstance(channel, str) and channel.strip():
                with self.server.lock:
                    self.server.active_channel = channel.strip()[:48]
                    if self.server.active_channel != "search":
                        self.server.active_query = ""
                    changed = True

            custom_ids = body.get("custom_slide_ids")
            if isinstance(custom_ids, list) and custom_ids:
                label = str(body.get("label") or "Pinned selection")[:80]
                with self.server.lock:
                    self.server.search_slide_ids = [str(x) for x in custom_ids[:80]]
                    self.server.active_query = label
                    self.server.active_channel = "search"
                    changed = True

            if "interval_sec" in body:
                try:
                    iv = int(body["interval_sec"])
                    if iv in ALLOWED_INTERVALS:
                        with self.server.lock:
                            self.server.interval_sec = iv
                            changed = True
                except (TypeError, ValueError):
                    pass

            if isinstance(body.get("paused"), bool):
                with self.server.lock:
                    self.server.paused = body["paused"]
                    changed = True

            if isinstance(body.get("show_captions"), bool):
                with self.server.lock:
                    self.server.show_captions = body["show_captions"]
                    changed = True

            if isinstance(body.get("show_clock"), bool):
                with self.server.lock:
                    self.server.show_clock = body["show_clock"]
                    changed = True

            action = body.get("action")
            if action in ("next", "prev", "goto"):
                with self.server.lock:
                    self.server.command_seq += 1
                    self.server.last_command = {
                        "seq": self.server.command_seq,
                        "action": action,
                        "slide_id": str(body.get("slide_id") or "")[:64] or None,
                    }
                    changed = True

            if changed:
                with self.server.lock:
                    self.server.revision += 1

            self.send_json({"ok": True, "state": self.server.build_state_payload()})

        elif path == "/api/hide":
            target_id = str(body.get("id") or "").strip()[:80]
            if not target_id:
                self.send_json({"error": "Missing slide id"}, 400)
                return
            hide_flag = bool(body.get("hidden", True))
            with self.server.lock:
                if hide_flag:
                    self.server._hidden_ids.add(target_id)
                else:
                    self.server._hidden_ids.discard(target_id)
                self.server._save_hidden()
                self.server.revision += 1
            self.send_json(
                {
                    "ok": True,
                    "id": target_id,
                    "hidden": hide_flag,
                    "hidden_ids": sorted(self.server._hidden_ids),
                    "state": self.server.build_state_payload(),
                }
            )
        else:
            self.send_json({"error": "Not found"}, 404)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Use your Wi-Fi IP (or 0.0.0.0) to expose to the LAN")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--photos", type=Path, help="Serve curated data/album directory or a plain JPG/PNG/WebP folder")
    args = parser.parse_args()

    chosen_photos = args.photos
    if chosen_photos is None and (DEFAULT_CURATED_DIR / "manifest.json").is_file():
        chosen_photos = DEFAULT_CURATED_DIR

    if chosen_photos and not chosen_photos.is_dir():
        parser.error("Photo directory does not exist")

    server = AlbumServer((args.host, args.port), chosen_photos)
    base_url = f"http://{args.host}:{server.server_port}"
    print(f"Ambient Frame: {base_url}/", flush=True)
    print(f"Gallery UI:    {base_url}/gallery", flush=True)
    print(f"Phone Remote:  {base_url}/remote", flush=True)
    if server.is_curated():
        st = server.build_state_payload()
        print(f"Loaded curated iPhone album ({len(st['slides'])} active slides across {len(st['channels'])} channels).", flush=True)
    print("LAN viewers can browse active slides and control playback. No external uploads.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

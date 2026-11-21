#!/usr/bin/env python3
"""Curate an ambient Echo Show / multi-device photo album from the local iPhone catalog.

Pipeline stages:
1. Filter `curation_catalog.json` to high-potential still images (~1,400 candidates).
2. Run an OpenAI Vision Ambient Frame Pass (`gpt-6.1-sol`, `store: false`, `detail: low`)
   through the local Quotaswitch gateway (`http://127.0.0.1:18455/v1`), checkpointing every
   50 items to `data/ambient_vision_cache.json`.
3. Apply strict kitchen/living-room safety gates and keep every temporally diverse winner
   above the frame-score bar across 6 channels (`featured`, `family_felix`, `travel_outdoors`,
   `food_gatherings`, `scenic_art`, `pets`).
4. Export crisp sRGB JPEGs (`full_2048` from `usb_originals/` or live `USBCameraBridge` IPC,
   gracefully falling back to enhanced `thumb_640` thumbnails with `--upgrade-originals`
   support whenever the iPhone is reconnected).
5. Pair harmonious portrait winners into pre-rendered `1600x960` (16:10) museum-matte
   side-by-side diptych slides (`data/album/diptychs/`) using `focal_xy` subject framing.
6. Compute `text-embedding-3-large` semantic vectors (`data/album/album_embeddings.npz`)
   for instant natural-language playlist creation on any device.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import urllib.error
import urllib.request
import uuid

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "private" / "profile.json"
PROFILE: Dict[str, Any] = json.loads(PROFILE_PATH.read_text(encoding="utf-8")) if PROFILE_PATH.is_file() else {}

CURATOR_REPO = Path(PROFILE["baby_repo"]) if PROFILE.get("baby_repo") else None
DEFAULT_CATALOG = Path(PROFILE["catalog_path"]) if PROFILE.get("catalog_path") else (ROOT / "data/curation_catalog.json")
DEFAULT_USB_ORIGINALS = Path(PROFILE["usb_originals_dir"]) if PROFILE.get("usb_originals_dir") else (ROOT / "data/usb_originals")
DEFAULT_USB_DAEMON = Path(PROFILE["usb_daemon_dir"]) if PROFILE.get("usb_daemon_dir") else (ROOT / "data/usb_daemon")
DEFAULT_OUT_DIR = ROOT / "data/album"
DEFAULT_VISION_CACHE = ROOT / "data/ambient_vision_cache.json"
DEFAULT_GATEWAY = os.environ.get("OPENAI_BASE_URL") or PROFILE.get("openai_base_url") or "https://api.openai.com/v1"
GATEWAY_TOKEN = os.environ.get("OPENAI_API_KEY") or PROFILE.get("openai_api_key") or ""
DEFAULT_VISION_MODEL = os.environ.get("VISION_MODEL") or PROFILE.get("vision_model") or "gpt-4.1-mini"

SUBJECT_NAME = str(PROFILE.get("subject_name") or "").strip()
SUBJECT_BIRTH_DATE = date.fromisoformat(PROFILE["subject_birth_date"]) if PROFILE.get("subject_birth_date") else None
FAMILY_CHANNEL_ID = "family"
FAMILY_CHANNEL_NAME = PROFILE.get("family_channel_name") or "Family & Friends"
FAMILY_CHANNEL_DESC = PROFILE.get("family_channel_description") or "Warm family portraits, gatherings, and milestones"
SRGB_PROFILE = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"

VALID_CHANNELS = (
    "featured",
    FAMILY_CHANNEL_ID,
    "family",
    "travel_outdoors",
    "food_gatherings",
    "scenic_art",
    "pets",
)
PET_PATTERN = re.compile(r"\b(dogs?|puppy|puppies|pets?|cats?|kitten|corgi|retriever|husky|shiba|poodle|doodle)\b", re.IGNORECASE)
VALID_PALETTES = (
    "warm_gold",
    "alpine_cool",
    "lush_green",
    "soft_neutral",
    "vibrant",
)

UNSAFE_TEXT_PATTERN = re.compile(
    r"\b("
    r"nicu|hospital|incubator|cannula|nasal tube|feeding tube|iv line|catheter|"
    r"medical monitor|vital signs|heart monitor|pulse oximeter|clinical|incision|"
    r"hospital bracelet|patient wristband|ultrasound printout|medical chart|"
    r"receipt|invoice|screenshot|otp|qr code|barcode|whiteboard|tax form|"
    r"shirtless|underwear|bathroom mirror|toilet|diaper rash"
    r")\b",
    re.IGNORECASE,
)

CLICHE_WORDS = (
    "tapestry",
    "whimsical",
    "symphony",
    "testament",
    "ethereal",
    "pure bliss",
    "magical moment",
    "timeless",
)

AMBIENT_SYSTEM_PROMPT = """You curate personal family and travel photographs for an always-on Kitchen Echo Show and living-room ambient frame seen daily by family and dinner guests.

Evaluate each candidate thumbnail and return strict JSON only:
{
  "ambient_safe": boolean,
  "frame_score": number,
  "channels": ["featured" | "''' + '" | "'.join(["family", "travel_outdoors", "food_gatherings", "scenic_art", "pets"]) + '''"],
  "focal_xy": [number, number],
  "color_palette": "warm_gold" | "alpine_cool" | "lush_green" | "soft_neutral" | "vibrant",
  "visual_theme": string,
  "display_caption": string
}

Rules:
1. "ambient_safe" MUST be false for ANY of:
   - Hospital/NICU scenes, medical tubes, wires, monitors, incubators, hospital bracelets, or clinical close-ups.
   - Shirtless adults, bathroom/mirror selfies, intimate/underwear shots, naked bath/diaper-change shots, or anything private/awkward in front of guests.
   - Phone/computer screens, text screenshots, menus, signs/labels/receipts/documents, QR codes.
   - Messy clutter, laundry/boxes, blurry motion, closed eyes / mid-blink, or awkward accidental framing.
   Only joyful, warm, scenic, appetizing, artistic, or heartwarming photos get "ambient_safe": true.
2. "frame_score" (1.0 to 10.0): visual beauty on a countertop frame (lighting, composition, color harmony, subject clarity, emotional warmth). Reserve >= 8.5 for standout gallery-worthy shots.
3. "channels": 1 to 3 matching channels. Include "featured" ONLY when ambient_safe is true AND frame_score >= 8.5.
4. "focal_xy": [x, y] in 0.0..1.0 indicating the primary face(s) or key subject center (0,0 = top-left, 1,1 = bottom-right) so smart crops never clip heads or faces.
5. "color_palette": dominant mood ("warm_gold", "alpine_cool", "lush_green", "soft_neutral", or "vibrant").
6. "visual_theme": concise snake_case motif (e.g. "snow_mountain", "baby_cuddle", "sunset_coast", "shared_dinner", "cafe_pastry", "festive_lights", "dog_portrait", "coastal_walk", "autumn_trees").
7. "display_caption": warm, concrete, natural 3-7 word gallery title for a subtle corner label (e.g. "Afternoon powder beneath pine ridges", "Sharing hotpot around the table", "Morning cuddles in knit stripes"). Avoid cheesy AI clichés ("pure bliss", "magical", "timeless", "tapestry") and never invent personal names not present in metadata."""


def parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    txt = value.strip()
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def season_for_month(month: int) -> str:
    if month in (12, 1, 2):
        return "Winter"
    if month in (3, 4, 5):
        return "Spring"
    if month in (6, 7, 8):
        return "Summer"
    return "Autumn"


def format_date_badge(dt: Optional[datetime], is_subject: bool = False) -> Tuple[str, str]:
    """Return (human_date_label, subject_age_badge)."""
    if not dt:
        return ("Archive", "")
    human_date = dt.strftime("%b %Y")
    age_label = ""
    cal = dt.date()
    if is_subject and SUBJECT_NAME and SUBJECT_BIRTH_DATE and cal >= SUBJECT_BIRTH_DATE:
        days = (cal - SUBJECT_BIRTH_DATE).days
        if days < 14:
            age_label = f"{SUBJECT_NAME} · {max(1, days)}d"
        elif days < 84:
            age_label = f"{SUBJECT_NAME} · {days // 7} wks"
        else:
            mos = max(1, round(days / 30.4))
            age_label = f"{SUBJECT_NAME} · {mos} mos"
    return (human_date, age_label)


def compute_dhash_hex(im: Image.Image) -> str:
    px = im.convert("L").resize((9, 8), Image.Resampling.BILINEAR).tobytes()
    bits = 0
    for row in range(8):
        off = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if px[off + col] > px[off + col + 1] else 0)
    return f"{bits:016x}"


def scan_photo_folder(source_dir: Path, thumbs_dir: Path) -> List[Dict[str, Any]]:
    """Ingest a plain folder of iPhone/camera photos into catalog records with 640px thumbs & dHash."""
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    exts = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
    items: List[Dict[str, Any]] = []
    for p in sorted(source_dir.rglob("*")):
        if not p.is_file() or p.is_symlink() or p.suffix.lower() not in exts:
            continue
        rel_id = str(p.relative_to(source_dir)).replace(os.sep, "__")
        thumb_path = thumbs_dir / f"{hashlib.sha1(rel_id.encode('utf-8')).hexdigest()[:14]}.jpg"
        if not thumb_path.is_file():
            transcode_to_srgb_jpeg(p, thumb_path, max_dim=640, enhance_thumb=False)
        if not thumb_path.is_file():
            continue
        created_iso = None
        try:
            with Image.open(p) as raw_im:
                ex = raw_im.getexif()
                dt_str = ex.get(0x9003) or ex.get(0x0132) or ex.get_ifd(0x8769).get(0x9003)
                if dt_str:
                    created_iso = datetime.strptime(str(dt_str)[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            created_iso = None
        if not created_iso:
            created_iso = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
        with Image.open(thumb_path) as tim:
            w, h = tim.size
            dh = compute_dhash_hex(tim)
        items.append(
            {
                "local_id": rel_id,
                "media_type": "image",
                "category": "highlight",
                "creation_date": created_iso,
                "width": w,
                "height": h,
                "aspect_ratio": round(w / float(max(1, h)), 4),
                "source_path": str(p.resolve()),
                "thumb_path": str(thumb_path),
                "dhash_hex": dh,
                "aesthetic_score": 8.0,
                "is_favorite": False,
                "scene_tags": [],
                "caption": "",
            }
        )
    return items


def hamming_hex(a: str, b: str) -> int:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return 64


class LocalGateway:
    """OpenAI-compatible client bound strictly to local Quotaswitch gateway with store=False."""

    def __init__(self, base_url: str = DEFAULT_GATEWAY, token: str = GATEWAY_TOKEN, timeout: int = 75) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.lock = threading.Lock()
        self.usage_stats = {
            "vision_calls": 0,
            "vision_cached_hits": 0,
            "vision_prompt_tokens": 0,
            "vision_completion_tokens": 0,
            "embedding_calls": 0,
            "embedding_inputs": 0,
            "embedding_tokens": 0,
        }

    def post_json(self, path: str, payload: Dict[str, Any], retries: int = 3) -> Dict[str, Any]:
        raw_body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            req = urllib.request.Request(
                f"{self.base_url}{path}",
                data=raw_body,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "ignore")[:320]
                last_err = RuntimeError(f"HTTP {exc.code} on {path}: {detail}")
                if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                    time.sleep(1.4 * (attempt + 1))
                    continue
                raise last_err from exc
            except Exception as exc:
                last_err = exc
                if attempt + 1 < retries:
                    time.sleep(1.4 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(f"Gateway request failed on {path}: {last_err}")

    def evaluate_ambient_photo(self, item: Dict[str, Any], model: str = DEFAULT_VISION_MODEL) -> Dict[str, Any]:
        thumb_path = Path(item["thumb_path"])
        b64 = base64.b64encode(thumb_path.read_bytes()).decode("ascii")
        dt = parse_iso_dt(item.get("creation_date"))
        post_birth = bool(SUBJECT_BIRTH_DATE and dt and dt.date() >= SUBJECT_BIRTH_DATE)
        is_subj = bool((item.get("is_subject_moment")) and post_birth)
        meta_hint: Dict[str, Any] = {
            "date": (item.get("creation_date") or "")[:10],
            "aspect_ratio": round(float(item.get("aspect_ratio") or 1.0), 3),
            "is_favorite": bool(item.get("is_favorite")),
            "prior_caption": item.get("caption") or "",
            "prior_tags": (item.get("scene_tags") or [])[:6],
            "prior_aesthetic": item.get("aesthetic_score"),
        }
        if is_subj and SUBJECT_NAME:
            meta_hint["milestone_subject"] = SUBJECT_NAME
        payload = {
            "model": model,
            "store": False,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": AMBIENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Evaluate this candidate photo for the ambient frame and return valid JSON only "
                                "(ambient_safe, frame_score, channels, focal_xy, color_palette, visual_theme, display_caption). "
                                f"Metadata JSON: {json.dumps(meta_hint)}"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
                        },
                    ],
                },
            ],
        }
        resp = self.post_json("/chat/completions", payload)
        usage = resp.get("usage") or {}
        with self.lock:
            self.usage_stats["vision_calls"] += 1
            self.usage_stats["vision_prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            self.usage_stats["vision_completion_tokens"] += int(usage.get("completion_tokens") or 0)
        content = resp["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        parsed["_usage"] = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
        }
        return parsed

    def embed_texts(self, texts: List[str], model: str = "text-embedding-3-large", batch_size: int = 64) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        all_vecs: List[List[float]] = []
        for start in range(0, len(texts), batch_size):
            chunk = texts[start : start + batch_size]
            resp = self.post_json("/embeddings", {"model": model, "input": chunk})
            usage = resp.get("usage") or {}
            with self.lock:
                self.usage_stats["embedding_calls"] += 1
                self.usage_stats["embedding_inputs"] += len(chunk)
                self.usage_stats["embedding_tokens"] += int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
            rows = sorted(resp["data"], key=lambda r: r["index"])
            for row in rows:
                all_vecs.append(row["embedding"])
        arr = np.asarray(all_vecs, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        return arr / norms


def _has_subject_flag(item: Dict[str, Any]) -> Tuple[bool, bool]:
    flag = bool(item.get("is_subject_moment"))
    keep = bool(item.get("keep_for_film", True))
    return flag, keep


def filter_candidates(catalog_items: List[Dict[str, Any]], limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Filter catalog items down to high-potential ambient still-image candidates."""
    candidates: List[Dict[str, Any]] = []
    for item in catalog_items:
        if item.get("media_type") != "image":
            continue
        cat = item.get("category") or ""
        if cat in ("receipt_or_doc", "ephemeral_clutter", "burst_extra"):
            continue
        if item.get("is_screenshot") or item.get("screenshot_signal"):
            continue
        subj_flag, subj_keep = _has_subject_flag(item)
        is_peaceful_sleep = subj_flag and subj_keep
        if item.get("eyes_open") is False and not is_peaceful_sleep:
            continue
        tpath = item.get("thumb_path")
        if not tpath or not os.path.isfile(tpath):
            continue
        prior_text = f"{item.get('caption') or ''} {' '.join(item.get('scene_tags') or [])}"
        if UNSAFE_TEXT_PATTERN.search(prior_text):
            continue
        if subj_flag and not subj_keep:
            continue

        year = (item.get("creation_date") or "")[:4]
        score = float(item.get("aesthetic_score") or 0.0)
        is_fav = bool(item.get("is_favorite"))
        if (
            cat == "highlight"
            or year in ("2019", "2020", "2021", "2022")
            or is_fav
            or score >= 6.0
        ):
            candidates.append(item)

    # Sort deterministically so `--limit` samples across high-scoring diverse years
    candidates.sort(
        key=lambda x: (
            -(float(x.get("aesthetic_score") or 0.0) + (0.4 if x.get("is_favorite") else 0.0)),
            x.get("creation_date") or "",
            x.get("local_id") or "",
        )
    )
    if limit is not None:
        return candidates[:limit]
    return candidates


def normalize_vision_result(item: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize model output and enforce strict ambient safety & channel invariants."""
    safe = bool(raw.get("ambient_safe", False))
    try:
        fscore = float(raw.get("frame_score", 5.0))
    except (TypeError, ValueError):
        fscore = 5.0
    fscore = round(min(max(fscore, 1.0), 10.0), 2)

    disp_caption = str(raw.get("display_caption") or item.get("caption") or "").strip()
    disp_caption = re.sub(r"\s+", " ", disp_caption).strip(" .\"'")
    for bad in CLICHE_WORDS:
        if bad in disp_caption.lower():
            disp_caption = str(item.get("caption") or disp_caption).strip(" .")
            break
    if len(disp_caption.split()) > 10:
        disp_caption = " ".join(disp_caption.split()[:8])

    combined_text = f"{disp_caption} {item.get('caption') or ''} {' '.join(item.get('scene_tags') or [])}"
    if UNSAFE_TEXT_PATTERN.search(combined_text):
        safe = False
    subj_flag, subj_keep = _has_subject_flag(item)
    is_peaceful_sleep = subj_flag and subj_keep
    if (item.get("eyes_open") is False and not is_peaceful_sleep) or item.get("is_screenshot") or item.get("screenshot_signal"):
        safe = False

    raw_channels = [c for c in (raw.get("channels") or [])]
    channels = list(dict.fromkeys(c for c in raw_channels if c in VALID_CHANNELS))

    dt = parse_iso_dt(item.get("creation_date"))
    is_subject = bool(subj_flag and subj_keep and SUBJECT_BIRTH_DATE and dt and dt.date() >= SUBJECT_BIRTH_DATE)
    if is_subject and FAMILY_CHANNEL_ID not in channels:
        channels.append(FAMILY_CHANNEL_ID)

    if safe and fscore >= 8.5:
        if "featured" not in channels:
            channels.insert(0, "featured")
    else:
        channels = [c for c in channels if c != "featured"]

    pet_text = f"{raw.get('visual_theme') or ''} {' '.join(item.get('scene_tags') or [])}".replace("_", " ")
    if PET_PATTERN.search(pet_text) and "pets" not in channels:
        channels.append("pets")

    if not channels:
        channels = ["scenic_art"]

    focal = raw.get("focal_xy")
    if isinstance(focal, (list, tuple)) and len(focal) >= 2:
        try:
            fx = round(min(max(float(focal[0]), 0.08), 0.92), 3)
            fy = round(min(max(float(focal[1]), 0.08), 0.92), 3)
        except (TypeError, ValueError):
            fx, fy = 0.5, 0.42
    else:
        fx, fy = 0.5, 0.42

    palette = str(raw.get("color_palette") or "soft_neutral").strip().lower()
    if palette not in VALID_PALETTES:
        palette = "soft_neutral"

    theme = re.sub(r"[^a-z0-9_]+", "_", str(raw.get("visual_theme") or "everyday_moment").strip().lower()).strip("_")
    if not theme:
        theme = "everyday_moment"

    return {
        "ambient_safe": safe,
        "frame_score": fscore,
        "channels": channels,
        "focal_xy": [fx, fy],
        "color_palette": palette,
        "visual_theme": theme[:36],
        "display_caption": disp_caption or "Warm memory",
        "_usage": raw.get("_usage") or {"prompt_tokens": 0, "completion_tokens": 0},
    }


def run_ambient_vision_pass(
    gateway: LocalGateway,
    candidates: List[Dict[str, Any]],
    cache_path: Path,
    model: str = "gpt-6.1-sol",
    workers: int = 28,
) -> Dict[str, Dict[str, Any]]:
    """Evaluate candidates concurrently with atomic checkpointing every 50 items."""
    cache: Dict[str, Dict[str, Any]] = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    lock = threading.Lock()
    newly_done = 0
    t0 = time.time()

    def save_checkpoint(force: bool = False) -> None:
        nonlocal newly_done
        with lock:
            if not force and (newly_done == 0 or newly_done % 50 != 0):
                return
            snap = dict(cache)
            done_now = newly_done
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(cache_path)
        elapsed = max(0.1, time.time() - t0)
        print(f"  [vision] checkpoint saved ({len(snap)} cached, +{done_now} new in {elapsed:.1f}s)", flush=True)

    todo: List[Dict[str, Any]] = []
    results: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        lid = item["local_id"]
        ck = f"{model}::{lid}::{item.get('dhash_hex') or ''}"
        if ck in cache:
            with gateway.lock:
                gateway.usage_stats["vision_cached_hits"] += 1
            results[lid] = normalize_vision_result(item, cache[ck])
        else:
            todo.append(item)

    print(
        f"[vision] {len(candidates)} candidates ({len(results)} cached, {len(todo)} to evaluate via {model} @ {workers} threads)",
        flush=True,
    )

    def worker(item: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        nonlocal newly_done
        lid = item["local_id"]
        ck = f"{model}::{lid}::{item.get('dhash_hex') or ''}"
        try:
            raw = gateway.evaluate_ambient_photo(item, model=model)
        except Exception as exc:
            print(f"  [vision-warn] {lid} failed ({exc}); marking ambient_safe=False (not cached)", flush=True)
            norm = normalize_vision_result(
                item,
                {"ambient_safe": False, "frame_score": 1.0, "channels": [], "display_caption": ""},
            )
            return lid, norm
        norm = normalize_vision_result(item, raw)
        with lock:
            cache[ck] = norm
            newly_done += 1
        save_checkpoint(force=False)
        return lid, norm

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = [pool.submit(worker, it) for it in todo]
            for fut in as_completed(futs):
                lid, norm = fut.result()
                results[lid] = norm
        save_checkpoint(force=True)

    return results


def select_curated_winners(
    candidates: List[Dict[str, Any]],
    vision_map: Dict[str, Dict[str, Any]],
    usb_originals_dir: Path,
    target_count: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Select diverse, ambient-safe winners; `target_count` optionally caps them with year/channel balancing."""
    existing_originals = set()
    if usb_originals_dir.is_dir():
        for p in usb_originals_dir.iterdir():
            if p.suffix.lower() in (".jpg", ".jpeg", ".heic", ".heif", ".png"):
                existing_originals.add(p.name)
                existing_originals.add(p.stem)

    qualified: List[Dict[str, Any]] = []
    for item in candidates:
        lid = item["local_id"]
        vis = vision_map.get(lid)
        if not vis or not vis.get("ambient_safe"):
            continue
        year = (item.get("creation_date") or "")[:4] or "unknown"
        min_score = 6.8 if year in ("2019", "2020", "2021", "2022") else 7.2
        if vis["frame_score"] < min_score:
            continue

        has_orig = lid in existing_originals or Path(lid).stem in existing_originals
        a_score = float(item.get("aesthetic_score") or 7.0)
        rank_score = (
            vis["frame_score"] * 0.74
            + a_score * 0.18
            + (0.35 if item.get("is_favorite") else 0.0)
            + (0.18 if has_orig else 0.0)
        )
        enriched = dict(item)
        enriched["_vision"] = vis
        enriched["_rank_score"] = round(rank_score, 4)
        enriched["_year"] = year
        enriched["_has_local_orig"] = has_orig
        qualified.append(enriched)

    qualified.sort(key=lambda x: (-x["_rank_score"], x.get("creation_date") or "", x["local_id"]))

    # Enforce temporal event diversity + near-duplicate dHash guard
    event_counts: Counter[str] = Counter()
    day_counts: Counter[str] = Counter()
    year_buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    kept_dhashes: List[str] = []

    for cand in qualified:
        dt = parse_iso_dt(cand.get("creation_date"))
        if dt:
            two_hour_slot = f"{dt.strftime('%Y-%m-%d')}_h{dt.hour // 2:02d}"
            day_key = dt.strftime("%Y-%m-%d")
        else:
            two_hour_slot = f"nodate_{cand['local_id']}"
            day_key = two_hour_slot

        fscore = cand["_vision"]["frame_score"]
        max_per_2h = 4 if (cand.get("is_favorite") and fscore >= 9.2) else 3
        max_per_day = 7 if (cand.get("is_favorite") and fscore >= 9.0) else 5

        if event_counts[two_hour_slot] >= max_per_2h:
            continue
        if day_counts[day_key] >= max_per_day:
            continue

        dh = cand.get("dhash_hex") or ""
        if dh and any(hamming_hex(dh, prev) <= 6 for prev in kept_dhashes[-80:]):
            continue

        event_counts[two_hour_slot] += 1
        day_counts[day_key] += 1
        if dh:
            kept_dhashes.append(dh)
        year_buckets[cand["_year"]].append(cand)

    if target_count is None:
        selected = [it for yr in sorted(year_buckets) for it in year_buckets[yr]]
        selected.sort(key=lambda x: (x.get("creation_date") or "", x["local_id"]))
        return selected

    # Stratified year allocation so 2019–2026 all shine
    selected: List[Dict[str, Any]] = []
    selected_ids: Set[str] = set()

    # Always include scarce early years (2019–2022) first
    for early_yr in sorted(y for y in year_buckets if y <= "2022"):
        for it in year_buckets[early_yr][:16]:
            selected.append(it)
            selected_ids.add(it["local_id"])

    all_diverse_pool = [it for yr in sorted(year_buckets) for it in year_buckets[yr]]
    all_diverse_pool.sort(key=lambda x: -x["_rank_score"])
    channel_floors = [(FAMILY_CHANNEL_ID, 42), ("food_gatherings", 42), ("scenic_art", 45)]
    for ch_name, floor_quota in channel_floors:
        ch_matches = [it for it in all_diverse_pool if ch_name in it["_vision"]["channels"]]
        taken = sum(1 for s in selected if ch_name in s["_vision"]["channels"])
        for it in ch_matches:
            if taken >= floor_quota or len(selected) >= target_count:
                break
            if it["local_id"] not in selected_ids:
                selected.append(it)
                selected_ids.add(it["local_id"])
                taken += 1

    remaining_slots = max(0, target_count - len(selected))
    main_years = sorted(y for y in year_buckets if y > "2022")
    if main_years and remaining_slots > 0:
        # Proportional + floor quota per year so 2023, 2024, 2025, 2026 are all well represented
        total_avail = sum(len(year_buckets[y]) for y in main_years)
        for yr in main_years:
            avail = year_buckets[yr]
            share = round(remaining_slots * (0.45 * (1.0 / len(main_years)) + 0.55 * (len(avail) / max(1, total_avail))))
            for it in avail[:share]:
                if it["local_id"] not in selected_ids and len(selected) < target_count:
                    selected.append(it)
                    selected_ids.add(it["local_id"])

    # Top-up any remaining slots by global rank_score
    if len(selected) < target_count:
        all_diverse = [it for yr in sorted(year_buckets) for it in year_buckets[yr] if it["local_id"] not in selected_ids]
        all_diverse.sort(key=lambda x: -x["_rank_score"])
        for it in all_diverse:
            if len(selected) >= target_count:
                break
            selected.append(it)
            selected_ids.add(it["local_id"])

    # Sort chronologically newest-first or balanced? Chronological ascending for stable IDs, we'll sequence slides smartly!
    selected.sort(key=lambda x: (x.get("creation_date") or "", x["local_id"]))
    return selected


def is_usb_bridge_alive(usb_daemon_dir: Path) -> bool:
    """Verify both live `USBCameraBridge` process AND ready state.json before queueing IPC."""
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "USBCameraBridge"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
    except Exception:
        return False

    state_file = usb_daemon_dir / "state.json"
    if not state_file.is_file():
        return False
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        return state.get("status") == "ready"
    except Exception:
        return False


def request_usb_originals_ipc(
    usb_daemon_dir: Path,
    usb_ids: List[str],
    download_dir: Path,
    batch_size: int = 20,
    per_batch_timeout: float = 180.0,
) -> Dict[str, Path]:
    """Follow the exact `local_stage.py:740-812` file-queue JSON IPC contract in safe batches."""
    if not usb_ids or not is_usb_bridge_alive(usb_daemon_dir):
        return {}

    requests_dir = usb_daemon_dir / "requests"
    responses_dir = usb_daemon_dir / "responses"
    requests_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)

    resolved: Dict[str, Path] = {}
    unique_ids = list(dict.fromkeys(usb_ids))
    for start in range(0, len(unique_ids), batch_size):
        if not is_usb_bridge_alive(usb_daemon_dir):
            break
        chunk = unique_ids[start : start + batch_size]
        req_id = f"dl_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        req_file = requests_dir / f"{req_id}.json"
        resp_file = responses_dir / f"{req_id}.json"
        req_file.write_text(
            json.dumps(
                {
                    "request_id": req_id,
                    "usb_ids": chunk,
                    "out_dir": str(download_dir.resolve()),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        deadline = time.time() + per_batch_timeout
        while time.time() < deadline:
            if resp_file.is_file():
                break
            time.sleep(0.8)
        if not resp_file.is_file():
            req_file.unlink(missing_ok=True)
            print(f"  [usb-ipc] batch {req_id} timed out; continuing with remaining batches", flush=True)
            continue
        try:
            resp = json.loads(resp_file.read_text(encoding="utf-8"))
            for uid, raw_path in (resp.get("downloaded") or {}).items():
                if raw_path and os.path.isfile(raw_path):
                    resolved[uid] = Path(raw_path)
            print(f"  [usb-ipc] downloaded {len(resolved)}/{len(unique_ids)} originals", flush=True)
        except Exception as exc:
            print(f"  [usb-ipc] malformed response {resp_file}: {exc}", flush=True)
            break
    return resolved


def find_existing_original(local_id: str, search_dirs: List[Path]) -> Optional[Path]:
    stem = Path(local_id).stem
    for d in search_dirs:
        if not d.is_dir():
            continue
        exact = d / local_id
        if exact.is_file() and exact.suffix.lower() in (".jpg", ".jpeg", ".heic", ".heif", ".png"):
            return exact
        for ext in (".JPG", ".jpg", ".HEIC", ".heic", ".JPEG", ".jpeg", ".PNG", ".png"):
            cand = d / f"{stem}{ext}"
            if cand.is_file():
                return cand
            cand2560 = d / f"{stem}_2560.jpg"
            if cand2560.is_file():
                return cand2560
    return None


def transcode_to_srgb_jpeg(src_path: Path, dst_path: Path, max_dim: int = 2048, enhance_thumb: bool = False) -> Tuple[int, int]:
    """Convert HEIC/JPG to crisp EXIF-oriented sRGB JPEG."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if src_path.suffix.lower() in (".heic", ".heif") or max_dim >= 1200 and not enhance_thumb:
        cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "92", "-Z", str(max_dim)]
        if os.path.exists(SRGB_PROFILE):
            cmd.extend(["--matchTo", SRGB_PROFILE])
        cmd.extend([str(src_path), "--out", str(dst_path)])
        proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        if proc.returncode == 0 and dst_path.is_file():
            with Image.open(dst_path) as im:
                im_oriented = ImageOps.exif_transpose(im)
                if im_oriented.size != im.size:
                    im_oriented.convert("RGB").save(dst_path, format="JPEG", quality=92, optimize=True)
                    return im_oriented.size
                return im.size

    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if max(im.size) > max_dim:
            im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        if enhance_thumb:
            # Subtle optical clarity polish so 640px thumbs sing on 1024x600 / 960x480 screens
            im = ImageEnhance.Contrast(im).enhance(1.03)
            im = ImageEnhance.Color(im).enhance(1.04)
            im = im.filter(ImageFilter.UnsharpMask(radius=1.05, percent=58, threshold=3))
        im.save(dst_path, format="JPEG", quality=92, subsampling=0, optimize=True)
        return im.size


def compute_accent_rgb(img_path: Path) -> List[int]:
    """Extract a rich, balanced ambient glow RGB from the image (avoiding muddy grey or harsh white)."""
    with Image.open(img_path) as im:
        small = im.convert("RGB").resize((24, 24), Image.Resampling.BILINEAR)
        arr = np.asarray(small, dtype=np.float32)  # (24, 24, 3)
    pixels = arr.reshape(-1, 3)
    lum = 0.2126 * pixels[:, 0] + 0.7152 * pixels[:, 1] + 0.0722 * pixels[:, 2]
    sat = pixels.max(axis=1) - pixels.min(axis=1)
    mask = (lum > 28) & (lum < 225) & (sat > 14)
    chosen = pixels[mask] if np.any(mask) else pixels
    med = np.median(chosen, axis=0)
    # Dampen slightly so ambient glow behind the photo never overpowers the photo itself
    r = int(min(max(round(med[0] * 0.78 + 14), 24), 175))
    g = int(min(max(round(med[1] * 0.78 + 14), 24), 170))
    b = int(min(max(round(med[2] * 0.78 + 16), 26), 180))
    return [r, g, b]


def subject_crop_resize(im: Image.Image, target_w: int, target_h: int, focal_xy: List[float]) -> Image.Image:
    """Scale and crop `im` to `(target_w, target_h)` centered around `focal_xy` without clipping faces."""
    src_w, src_h = im.size
    target_ar = target_w / float(target_h)
    src_ar = src_w / float(src_h)

    fx = min(max(float(focal_xy[0]), 0.05), 0.95)
    fy = min(max(float(focal_xy[1]), 0.05), 0.95)

    if src_ar > target_ar:
        # Wider than target: crop horizontally around fx
        crop_h = src_h
        crop_w = max(1, int(round(src_h * target_ar)))
        center_x = fx * src_w
        left = int(round(center_x - crop_w / 2.0))
        left = min(max(0, left), src_w - crop_w)
        box = (left, 0, left + crop_w, crop_h)
    else:
        # Taller than target: crop vertically around fy (bias slightly upward to protect heads)
        crop_w = src_w
        crop_h = max(1, int(round(src_w / target_ar)))
        head_safe_fy = max(0.08, fy - 0.04)
        center_y = head_safe_fy * src_h
        top = int(round(center_y - crop_h / 2.0))
        top = min(max(0, top), src_h - crop_h)
        box = (0, top, crop_w, top + crop_h)

    cropped = im.crop(box)
    return cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)


def compose_gallery_matte(
    canvas_size: Tuple[int, int],
    accent_rgb: Any,
    tiles_with_pos: List[Tuple[Image.Image, int, int]],
    *,
    blend: float = 0.14,
    rx_scale: float = 0.58,
    ry_scale: float = 0.62,
) -> Image.Image:
    """Composite one or more RGB photo tiles onto a #0b0e13 radial gallery matte with shadow and 1px hairline."""
    cw, ch = canvas_size
    base_matte = np.array([11.0, 14.0, 19.0], dtype=np.float32)  # #0b0e13
    rgb_vec = np.asarray(accent_rgb, dtype=np.float32)[:3]

    yy, xx = np.mgrid[0:ch, 0:cw].astype(np.float32)
    nx = (xx - cw * 0.5) / (cw * rx_scale)
    ny = (yy - ch * 0.5) / (ch * ry_scale)
    falloff = np.clip(1.0 - (nx * nx + ny * ny), 0.0, 1.0) ** 1.6
    bg_arr = base_matte[None, None, :] + falloff[:, :, None] * (rgb_vec[None, None, :] - base_matte[None, None, :]) * blend
    canvas = Image.fromarray(np.clip(bg_arr, 0, 255).astype(np.uint8), mode="RGB")

    shadow = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    for tile, x_left, y_top in tiles_with_pos:
        block = Image.new("RGBA", tile.size, (0, 0, 0, 118))
        shadow.paste(block, (x_left, y_top + 8))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=16))

    canvas_rgba = Image.alpha_composite(canvas.convert("RGBA"), shadow)
    overlay = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    hairline = (255, 255, 255, 32)
    for tile, x_left, y_top in tiles_with_pos:
        canvas_rgba.paste(tile.convert("RGBA"), (x_left, y_top))
        tw, th = tile.size
        draw.rectangle([x_left, y_top, x_left + tw - 1, y_top + th - 1], outline=hairline, width=1)

    return Image.alpha_composite(canvas_rgba, overlay).convert("RGB")


def render_diptych_jpeg(
    left_path: Path,
    right_path: Path,
    left_focal: List[float],
    right_focal: List[float],
    left_rgb: List[int],
    right_rgb: List[int],
    out_path: Path,
    canvas_size: Tuple[int, int] = (1600, 960),
) -> Tuple[int, int]:
    """Render a crisp 1600x960 (16:10) side-by-side portrait diptych on a #0b0e13 gallery matte."""
    cw, ch = canvas_size
    avg_accent = (np.array(left_rgb, dtype=np.float32) + np.array(right_rgb, dtype=np.float32)) * 0.5

    with Image.open(left_path) as im_l_raw, Image.open(right_path) as im_r_raw:
        im_l = ImageOps.exif_transpose(im_l_raw).convert("RGB")
        im_r = ImageOps.exif_transpose(im_r_raw).convert("RGB")

        ar_l = im_l.width / float(im_l.height)
        ar_r = im_r.width / float(im_r.height)
        mean_ar = (ar_l + ar_r) * 0.5

        card_h = ch - 96  # 864px tall (48px top/bottom margin)
        gutter = 28
        max_card_w = (cw - 152 - gutter) // 2  # 710px max width per portrait

        # Preserve natural portrait proportions if both are tall 9:16 shots so zero faces clip
        ideal_w = int(round(card_h * min(max(mean_ar, 0.64), 0.822)))
        card_w = min(max_card_w, ideal_w)

        tile_l = subject_crop_resize(im_l, card_w, card_h, left_focal)
        tile_r = subject_crop_resize(im_r, card_w, card_h, right_focal)

    total_w = card_w * 2 + gutter
    x_left = (cw - total_w) // 2
    x_right = x_left + card_w + gutter
    y_top = (ch - card_h) // 2

    final_rgb = compose_gallery_matte(
        canvas_size,
        avg_accent,
        [(tile_l, x_left, y_top), (tile_r, x_right, y_top)],
        blend=0.14,
        rx_scale=0.58,
        ry_scale=0.62,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    final_rgb.save(out_path, format="JPEG", quality=92, subsampling=0, optimize=True)
    return final_rgb.size


def pair_portrait_diptychs(single_records: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any], float]]:
    """Pair harmonious portrait winners into museum-grade side-by-side diptychs."""
    portraits = [r for r in single_records if r["aspect_ratio"] < 0.92]
    if len(portraits) < 2:
        return []

    compatible_palettes = {
        ("warm_gold", "soft_neutral"),
        ("soft_neutral", "warm_gold"),
        ("alpine_cool", "soft_neutral"),
        ("soft_neutral", "alpine_cool"),
        ("lush_green", "alpine_cool"),
        ("alpine_cool", "lush_green"),
        ("warm_gold", "vibrant"),
        ("vibrant", "warm_gold"),
    }

    scored_edges: List[Tuple[float, int, int]] = []
    for i in range(len(portraits)):
        a = portraits[i]
        dt_a = parse_iso_dt(a.get("creation_date"))
        rgb_a = np.array(a["accent_rgb"], dtype=np.float32)
        ch_a = set(a["channels"]) - {"featured"}
        for j in range(i + 1, len(portraits)):
            b = portraits[j]
            # Avoid near-duplicate frames side by side
            if hamming_hex(a.get("dhash_hex", ""), b.get("dhash_hex", "")) < 12:
                continue
            if a["display_caption"].lower() == b["display_caption"].lower():
                continue
            if abs(a["aspect_ratio"] - b["aspect_ratio"]) > 0.16:
                continue
            if a.get("is_subject", False) != b.get("is_subject", False):
                continue
            dt_b = parse_iso_dt(b.get("creation_date"))
            if dt_a and dt_b:
                sec_apart = abs((dt_a - dt_b).total_seconds())
                # Require at least 20 minutes separation so two shots of the exact same pose aren't paired side-by-side
                if sec_apart < 1200:
                    continue
                if abs((dt_a - dt_b).days) > 180:
                    continue

            affinity = 0.0
            if dt_a and dt_b:
                hours_apart = abs((dt_a - dt_b).total_seconds()) / 3600.0
                if hours_apart <= 18:
                    affinity += 4.2
                elif hours_apart <= 96:
                    affinity += 2.6
                elif dt_a.year == dt_b.year and dt_a.month == dt_b.month:
                    affinity += 1.1

            if a["visual_theme"] == b["visual_theme"]:
                affinity += 3.2
            shared_ch = ch_a & (set(b["channels"]) - {"featured"})
            if shared_ch:
                affinity += 1.8

            pal_a, pal_b = a["color_palette"], b["color_palette"]
            if pal_a == pal_b:
                affinity += 2.0
            elif (pal_a, pal_b) in compatible_palettes:
                affinity += 1.1
            else:
                affinity -= 1.2

            rgb_dist = float(np.linalg.norm(rgb_a - np.array(b["accent_rgb"], dtype=np.float32)))
            affinity += max(0.0, 1.5 - rgb_dist / 85.0)

            if a["resolution_tier"] == b["resolution_tier"]:
                affinity += 0.5

            affinity += (a["frame_score"] + b["frame_score"]) * 0.18
            if affinity >= 5.2:
                scored_edges.append((affinity, i, j))

    scored_edges.sort(key=lambda t: -t[0])
    used: Set[int] = set()
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any], float]] = []
    for score, i, j in scored_edges:
        if i in used or j in used:
            continue
        used.add(i)
        used.add(j)
        left, right = portraits[i], portraits[j]
        if (left.get("creation_date") or "") > (right.get("creation_date") or ""):
            left, right = right, left
        pairs.append((left, right, round(score, 3)))
    return pairs


def build_slide_embedding_doc(slide: Dict[str, Any]) -> str:
    dt = parse_iso_dt(slide.get("creation_date"))
    season = season_for_month(dt.month) if dt else ""
    year = str(dt.year) if dt else ""
    month_name = dt.strftime("%B %Y") if dt else ""
    channels_txt = ", ".join(slide.get("channels") or [])
    tags_txt = ", ".join(slide.get("tags") or [])
    badge = slide.get("age_badge") or ""
    subj_txt = f"{SUBJECT_NAME} family keepsake ({badge})" if (slide.get("is_subject") and SUBJECT_NAME) else badge
    kind_txt = "side-by-side portrait diptych" if slide.get("kind") == "diptych" else "photograph"
    return " | ".join(
        part
        for part in [
            slide.get("display_caption") or "",
            slide.get("search_detail") or "",
            f"{season} {month_name} {year}".strip(),
            slide.get("visual_theme", "").replace("_", " "),
            slide.get("color_palette", "").replace("_", " "),
            channels_txt,
            tags_txt,
            subj_txt,
            kind_txt,
        ]
        if part
    )


def interleave_slides_for_frame(slides: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order slides so consecutive frames vary themes/years while showcasing best shots early."""
    ordered = sorted(
        slides,
        key=lambda s: (
            -(s.get("frame_score", 8.0) + (0.35 if s.get("kind") == "diptych" else 0.0)),
            s.get("creation_date") or "",
            s["id"],
        ),
    )
    if len(ordered) <= 3:
        return ordered

    result: List[Dict[str, Any]] = []
    pool = list(ordered)
    while pool:
        if not result:
            result.append(pool.pop(0))
            continue
        prev = result[-1]
        best_idx = 0
        for idx, cand in enumerate(pool[:14]):
            same_day = (cand.get("creation_date") or "")[:10] == (prev.get("creation_date") or "")[:10]
            same_theme = cand.get("visual_theme") == prev.get("visual_theme")
            if not same_day and not same_theme:
                best_idx = idx
                break
            if not same_day:
                best_idx = idx
        result.append(pool.pop(best_idx))
    return result


def rerender_manifest_diptychs(
    album_dir: Path,
    photo_by_id: Dict[str, Dict[str, Any]],
    slides: List[Dict[str, Any]],
    *,
    only_changed_ids: Optional[Set[str]] = None,
    force_rebuild: bool = False,
) -> int:
    """Re-render missing or upgraded `1600x960` diptych JPEGs in place."""
    rebuilt = 0
    for slide in slides:
        if slide.get("kind") != "diptych":
            continue
        members = slide.get("member_ids") or []
        if len(members) < 2 or members[0] not in photo_by_id or members[1] not in photo_by_id:
            continue
        left_id, right_id = members[0], members[1]
        rel = slide.get("rel_path") or slide["src"].lstrip("/")
        dip_dst = album_dir / rel
        if only_changed_ids is not None:
            if left_id not in only_changed_ids and right_id not in only_changed_ids:
                continue
        elif not force_rebuild and dip_dst.is_file():
            continue
        lp, rp = photo_by_id[left_id], photo_by_id[right_id]
        render_diptych_jpeg(
            album_dir / lp["rel_path"],
            album_dir / rp["rel_path"],
            lp["focal_xy"],
            rp["focal_xy"],
            lp["accent_rgb"],
            rp["accent_rgb"],
            dip_dst,
        )
        slide["resolution_tier"] = (
            "full_2048"
            if lp.get("resolution_tier") == "full_2048" and rp.get("resolution_tier") == "full_2048"
            else ("mixed_2048" if "full_2048" in (lp.get("resolution_tier"), rp.get("resolution_tier")) else "thumb_640")
        )
        rebuilt += 1
    return rebuilt


def upgrade_originals_in_place(
    out_dir: Path,
    usb_originals_dir: Path,
    usb_daemon_dir: Path,
) -> int:
    """Upgrade any `thumb_640` entries in `manifest.json` when iPhone + USBCameraBridge are live."""
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found at {manifest_path}. Run full extraction first.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    photos: List[Dict[str, Any]] = manifest.get("photos") or []
    pending = [p for p in photos if p.get("resolution_tier") != "full_2048"]
    if not pending:
        print("[upgrade] All curated photos are already at full_2048 resolution!", flush=True)
        return 0

    usb_download_cache = out_dir.parent / "usb_downloads"
    search_dirs = [usb_originals_dir, usb_download_cache]

    bridge_live = is_usb_bridge_alive(usb_daemon_dir)
    print(f"[upgrade] {len(pending)} photos at thumb_640; USBCameraBridge live={bridge_live}", flush=True)

    missing_usb_ids = [
        p["local_id"]
        for p in pending
        if find_existing_original(p["local_id"], search_dirs) is None
    ]
    ipc_resolved: Dict[str, Path] = {}
    if bridge_live and missing_usb_ids:
        print(f"[upgrade] Requesting {len(missing_usb_ids)} full-res originals over USBCameraBridge...", flush=True)
        ipc_resolved = request_usb_originals_ipc(usb_daemon_dir, missing_usb_ids, usb_download_cache)

    upgraded_ids: Set[str] = set()
    for p in pending:
        orig = ipc_resolved.get(p["local_id"]) or find_existing_original(p["local_id"], search_dirs)
        if orig is None:
            continue
        dst = out_dir / p["rel_path"]
        w, h = transcode_to_srgb_jpeg(orig, dst, max_dim=2048, enhance_thumb=False)
        p["width"], p["height"] = w, h
        p["aspect_ratio"] = round(w / float(max(1, h)), 4)
        p["resolution_tier"] = "full_2048"
        p["accent_rgb"] = compute_accent_rgb(dst)
        upgraded_ids.add(p["id"])

    if upgraded_ids:
        photo_by_id = {p["id"]: p for p in photos}
        slides = manifest.get("slides") or []
        for slide in slides:
            if slide.get("kind") == "single" and slide.get("photo_id") in upgraded_ids:
                p = photo_by_id[slide["photo_id"]]
                slide["width"], slide["height"] = p["width"], p["height"]
                slide["resolution_tier"] = "full_2048"
                slide["accent_rgb"] = p["accent_rgb"]
        rerender_manifest_diptychs(out_dir, photo_by_id, slides, only_changed_ids=upgraded_ids)

        manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        manifest["stats"]["full_2048_photos"] = sum(1 for p in photos if p["resolution_tier"] == "full_2048")
        manifest["stats"]["thumb_640_photos"] = sum(1 for p in photos if p["resolution_tier"] != "full_2048")
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"[upgrade] Upgraded {len(upgraded_ids)} curated photos to full_2048.", flush=True)
    return len(upgraded_ids)


def run_pipeline(args: argparse.Namespace) -> Dict[str, Any]:
    if args.upgrade_originals:
        count = upgrade_originals_in_place(args.out_dir, args.usb_originals, args.usb_daemon_dir)
        return {"upgraded": count}

    t_start = time.time()
    source_dir = getattr(args, "source_dir", None)
    if source_dir is not None:
        raw_items = scan_photo_folder(Path(source_dir), args.out_dir.parent / "folder_thumbs")
    else:
        catalog_data = json.loads(args.catalog.read_text(encoding="utf-8"))
        raw_items = catalog_data["items"] if isinstance(catalog_data, dict) else catalog_data

    candidates = filter_candidates(raw_items, limit=args.limit)
    print(f"[stage 1] Filtered {len(raw_items)} catalog assets -> {len(candidates)} still-image candidates", flush=True)

    gateway = LocalGateway(base_url=args.gateway)
    vision_map = run_ambient_vision_pass(
        gateway=gateway,
        candidates=candidates,
        cache_path=args.cache,
        model=args.model,
        workers=args.workers,
    )

    safe_total = sum(1 for v in vision_map.values() if v.get("ambient_safe"))
    print(f"[stage 2] Ambient safety gate passed {safe_total} / {len(candidates)} candidates", flush=True)

    target = min(args.target_count or safe_total, safe_total) if args.limit else args.target_count
    winners = select_curated_winners(
        candidates=candidates,
        vision_map=vision_map,
        usb_originals_dir=args.usb_originals,
        target_count=target,
    )
    print(f"[stage 3] Selected {len(winners)} temporally diverse winners across years", flush=True)

    out_dir: Path = args.out_dir
    photos_dir = out_dir / "photos"
    diptychs_dir = out_dir / "diptychs"
    photos_dir.mkdir(parents=True, exist_ok=True)
    diptychs_dir.mkdir(parents=True, exist_ok=True)

    usb_download_cache = out_dir.parent / "usb_downloads"
    search_dirs = [args.usb_originals, usb_download_cache]

    bridge_live = is_usb_bridge_alive(args.usb_daemon_dir)
    print(f"[stage 4] USBCameraBridge live process check: {bridge_live}", flush=True)
    ipc_resolved: Dict[str, Path] = {}
    if bridge_live:
        missing_usb = [
            w["local_id"]
            for w in winners
            if find_existing_original(w["local_id"], search_dirs) is None
        ]
        if missing_usb:
            print(f"[stage 4] Pulling {len(missing_usb)} originals from connected iPhone via USBCameraBridge...", flush=True)
            ipc_resolved = request_usb_originals_ipc(args.usb_daemon_dir, missing_usb, usb_download_cache)

    # Clean stale files from previous runs if rebuilding full album
    expected_photo_files: Set[str] = set()
    single_records: List[Dict[str, Any]] = []

    for item in winners:
        lid = item["local_id"]
        pid = f"p_{hashlib.sha1(lid.encode('utf-8')).hexdigest()[:12]}"
        fname = f"{pid}.jpg"
        expected_photo_files.add(fname)
        dst_path = photos_dir / fname

        src_override = Path(item["source_path"]) if item.get("source_path") and os.path.isfile(item["source_path"]) else None
        orig_path = src_override or ipc_resolved.get(lid) or find_existing_original(lid, search_dirs)
        if orig_path is not None:
            w, h = transcode_to_srgb_jpeg(orig_path, dst_path, max_dim=2048, enhance_thumb=False)
            res_tier = "full_2048"
        else:
            w, h = transcode_to_srgb_jpeg(Path(item["thumb_path"]), dst_path, max_dim=640, enhance_thumb=True)
            res_tier = "thumb_640"

        vis = item["_vision"]
        dt = parse_iso_dt(item.get("creation_date"))
        subj_flag, subj_keep = _has_subject_flag(item)
        is_subject = bool(subj_flag and subj_keep and SUBJECT_BIRTH_DATE and dt and dt.date() >= SUBJECT_BIRTH_DATE)
        date_label, age_badge = format_date_badge(dt, is_subject=is_subject)
        accent_rgb = compute_accent_rgb(dst_path)

        single_records.append(
            {
                "id": pid,
                "local_id": lid,
                "kind": "single",
                "src": f"/photos/{fname}",
                "rel_path": f"photos/{fname}",
                "width": w,
                "height": h,
                "aspect_ratio": round(w / float(max(1, h)), 4),
                "orientation": "portrait" if w < h * 0.94 else ("landscape" if w > h * 1.06 else "square"),
                "resolution_tier": res_tier,
                "creation_date": item.get("creation_date"),
                "year": (item.get("creation_date") or "")[:4],
                "month_day": (item.get("creation_date") or "")[5:10],
                "date_label": date_label,
                "age_badge": age_badge,
                "is_subject": is_subject,
                "is_favorite": bool(item.get("is_favorite")),
                "frame_score": vis["frame_score"],
                "aesthetic_score": float(item.get("aesthetic_score") or 0.0),
                "channels": vis["channels"],
                "focal_xy": vis["focal_xy"],
                "color_palette": vis["color_palette"],
                "visual_theme": vis["visual_theme"],
                "display_caption": vis["display_caption"],
                "search_detail": item.get("caption") or "",
                "tags": item.get("scene_tags") or [],
                "accent_rgb": accent_rgb,
                "dhash_hex": item.get("dhash_hex") or "",
            }
        )

    for existing_f in photos_dir.glob("*.jpg"):
        if existing_f.name not in expected_photo_files:
            existing_f.unlink(missing_ok=True)

    # Stage 5: Pair compatible portrait winners into 1600x960 museum diptychs
    paired = pair_portrait_diptychs(single_records)
    print(f"[stage 5] Rendering {len(paired)} side-by-side 1600x960 portrait diptychs...", flush=True)

    expected_diptych_files: Set[str] = set()
    diptych_slides: List[Dict[str, Any]] = []
    paired_member_ids: Set[str] = set()

    for left, right, pair_affinity in paired:
        pair_key = f"{left['local_id']}::{right['local_id']}"
        did = f"d_{hashlib.sha1(pair_key.encode('utf-8')).hexdigest()[:12]}"
        dfname = f"{did}.jpg"
        expected_diptych_files.add(dfname)
        dip_path = diptychs_dir / dfname

        dw, dh = render_diptych_jpeg(
            photos_dir / Path(left["rel_path"]).name,
            photos_dir / Path(right["rel_path"]).name,
            left["focal_xy"],
            right["focal_xy"],
            left["accent_rgb"],
            right["accent_rgb"],
            dip_path,
        )
        paired_member_ids.add(left["id"])
        paired_member_ids.add(right["id"])

        merged_channels = list(dict.fromkeys(left["channels"] + right["channels"]))
        avg_fscore = round((left["frame_score"] + right["frame_score"]) / 2.0, 2)
        if avg_fscore >= 8.5 and "featured" not in merged_channels:
            merged_channels.insert(0, "featured")
        elif avg_fscore < 8.5:
            merged_channels = [c for c in merged_channels if c != "featured"]
        merged_channels.append("diptychs")

        avg_rgb = [
            int(round((left["accent_rgb"][k] + right["accent_rgb"][k]) / 2.0))
            for k in range(3)
        ]

        # Clean combined caption: if same day/theme, use the higher-scored caption; else join gracefully
        if left["date_label"] == right["date_label"]:
            dip_date = left["date_label"]
        else:
            dip_date = f"{left['date_label']} · {right['date_label']}"

        primary = left if left["frame_score"] >= right["frame_score"] else right
        secondary = right if primary is left else left
        if left["visual_theme"] == right["visual_theme"] or (left["creation_date"] or "")[:10] == (right["creation_date"] or "")[:10]:
            dip_caption = primary["display_caption"]
        else:
            dip_caption = f"{left['display_caption']} · {right['display_caption']}"
            if len(dip_caption) > 64:
                dip_caption = primary["display_caption"]

        res_tier = (
            "full_2048"
            if left["resolution_tier"] == "full_2048" and right["resolution_tier"] == "full_2048"
            else ("mixed_2048" if "full_2048" in (left["resolution_tier"], right["resolution_tier"]) else "thumb_640")
        )

        diptych_slides.append(
            {
                "id": did,
                "kind": "diptych",
                "member_ids": [left["id"], right["id"]],
                "src": f"/diptychs/{dfname}",
                "rel_path": f"diptychs/{dfname}",
                "width": dw,
                "height": dh,
                "aspect_ratio": round(dw / float(dh), 4),
                "orientation": "landscape",
                "resolution_tier": res_tier,
                "creation_date": primary["creation_date"],
                "year": primary["year"],
                "month_day": primary["month_day"],
                "date_label": dip_date,
                "age_badge": primary["age_badge"] or secondary["age_badge"],
                "is_subject": left["is_subject"] or right["is_subject"],
                "is_favorite": left["is_favorite"] or right["is_favorite"],
                "frame_score": avg_fscore,
                "pair_affinity": pair_affinity,
                "channels": merged_channels,
                "color_palette": primary["color_palette"],
                "visual_theme": primary["visual_theme"],
                "display_caption": dip_caption,
                "search_detail": f"{left['display_caption']}; {left['search_detail']} | {right['display_caption']}; {right['search_detail']}",
                "tags": list(dict.fromkeys((left["tags"] or []) + (right["tags"] or [])))[:14],
                "accent_rgb": avg_rgb,
            }
        )

    for existing_d in diptychs_dir.glob("*.jpg"):
        if existing_d.name not in expected_diptych_files:
            existing_d.unlink(missing_ok=True)

    # Build Ambient Frame Slides:
    # - All diptych slides (turning 2 portraits into 1 full-frame 16:10 slide)
    # - All landscape/square single winners
    # - Unpaired portrait winners (plus top iconic favorite portraits >= 9.1 so they can also shine solo)
    frame_slides_raw: List[Dict[str, Any]] = list(diptych_slides)
    for rec in single_records:
        is_unpaired_portrait = rec["id"] not in paired_member_ids
        is_landscape_or_sq = rec["aspect_ratio"] >= 0.92
        is_iconic_solo = rec["is_favorite"] and rec["frame_score"] >= 9.2
        slide_copy = {
            "id": rec["id"],
            "photo_id": rec["id"],
            "kind": "single",
            "src": rec["src"],
            "rel_path": rec["rel_path"],
            "width": rec["width"],
            "height": rec["height"],
            "aspect_ratio": rec["aspect_ratio"],
            "orientation": rec["orientation"],
            "resolution_tier": rec["resolution_tier"],
            "creation_date": rec["creation_date"],
            "year": rec["year"],
            "month_day": rec["month_day"],
            "date_label": rec["date_label"],
            "age_badge": rec["age_badge"],
            "is_subject": rec["is_subject"],
            "is_favorite": rec["is_favorite"],
            "frame_score": rec["frame_score"],
            "channels": list(rec["channels"]),
            "focal_xy": rec["focal_xy"],
            "color_palette": rec["color_palette"],
            "visual_theme": rec["visual_theme"],
            "display_caption": rec["display_caption"],
            "search_detail": rec["search_detail"],
            "tags": rec["tags"],
            "accent_rgb": rec["accent_rgb"],
            "in_default_frame": bool(is_landscape_or_sq or is_unpaired_portrait or is_iconic_solo),
        }
        frame_slides_raw.append(slide_copy)

    for d_slide in diptych_slides:
        d_slide["in_default_frame"] = True

    sequenced_slides = interleave_slides_for_frame(frame_slides_raw)

    # Stage 6: Build OpenAI Semantic Search Embeddings (`text-embedding-3-large`)
    print(f"[stage 6] Computing text-embedding-3-large vectors for {len(sequenced_slides)} slides...", flush=True)
    embed_docs = [build_slide_embedding_doc(s) for s in sequenced_slides]
    embed_ids = [s["id"] for s in sequenced_slides]
    vectors = gateway.embed_texts(embed_docs, model="text-embedding-3-large")
    embeddings_path = out_dir / "album_embeddings.npz"
    np.savez_compressed(embeddings_path, ids=np.array(embed_ids), vectors=vectors)

    # Compute total vision tokens across entire cache + fresh calls for transparent reporting
    full_cache = json.loads(args.cache.read_text(encoding="utf-8")) if args.cache.is_file() else {}
    cached_prompt_toks = sum(int((v.get("_usage") or {}).get("prompt_tokens") or 0) for v in full_cache.values())
    cached_comp_toks = sum(int((v.get("_usage") or {}).get("completion_tokens") or 0) for v in full_cache.values())

    channel_defs = [
        {"id": "smart_frame", "name": "Ambient Mix", "description": "Landscape frames & portrait diptychs curated for wall and kitchen displays"},
        {"id": "featured", "name": "Featured", "description": "Highest-rated gallery standouts across every year"},
        {"id": FAMILY_CHANNEL_ID, "name": FAMILY_CHANNEL_NAME, "description": FAMILY_CHANNEL_DESC},
        {"id": "travel_outdoors", "name": "Mountains & Travel", "description": "Alpine snow, coastal horizons, and outdoor adventures"},
        {"id": "food_gatherings", "name": "Table & Gatherings", "description": "Shared home dinners, toasts, cafes, and favorite dishes"},
        {"id": "scenic_art", "name": "Scenic & Quiet", "description": "Architecture, light, flora, and calm compositions"},
        {"id": "pets", "name": "Dogs & Pets", "description": "Pups, pets, and furry companions"},
        {"id": "diptychs", "name": "Portrait Diptychs", "description": "Harmonious side-by-side 16:10 portrait pairings"},
        {"id": "on_this_day", "name": "This Season", "description": "Memories captured around this week and month across past years"},
    ]

    # Strip internal dhash from public manifest
    clean_photos = [{k: v for k, v in p.items() if k != "dhash_hex"} for p in single_records]

    year_dist = dict(sorted(Counter(p["year"] for p in clean_photos).items()))
    orient_dist = dict(Counter(p["orientation"] for p in clean_photos))
    channel_counts = {
        ch["id"]: (
            sum(1 for s in sequenced_slides if s.get("in_default_frame"))
            if ch["id"] == "smart_frame"
            else sum(1 for s in sequenced_slides if ch["id"] in (s.get("channels") or []))
        )
        for ch in channel_defs
    }

    manifest = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_catalog_count": len(raw_items),
        "candidate_evaluated_count": len(candidates),
        "ambient_safe_count": safe_total,
        "usb_bridge_live_during_export": bridge_live,
        "channels": channel_defs,
        "stats": {
            "curated_photos": len(clean_photos),
            "diptych_slides": len(diptych_slides),
            "total_searchable_slides": len(sequenced_slides),
            "default_frame_slides": sum(1 for s in sequenced_slides if s.get("in_default_frame")),
            "full_2048_photos": sum(1 for p in clean_photos if p["resolution_tier"] == "full_2048"),
            "thumb_640_photos": sum(1 for p in clean_photos if p["resolution_tier"] != "full_2048"),
            "years": year_dist,
            "orientations": orient_dist,
            "channel_counts": channel_counts,
            "elapsed_sec": round(time.time() - t_start, 2),
            "api_usage": {
                **gateway.usage_stats,
                "cumulative_vision_prompt_tokens": cached_prompt_toks,
                "cumulative_vision_completion_tokens": cached_comp_toks,
            },
        },
        "photos": clean_photos,
        "slides": sequenced_slides,
    }

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[done] Wrote {manifest_path} and {embeddings_path} in {manifest['stats']['elapsed_sec']}s", flush=True)
    print(json.dumps(manifest["stats"], indent=2), flush=True)
    return manifest["stats"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=None, help="Plain directory of iPhone/camera photos to scan & curate directly")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--usb-originals", type=Path, default=DEFAULT_USB_ORIGINALS)
    parser.add_argument("--usb-daemon-dir", type=Path, default=DEFAULT_USB_DAEMON)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--cache", type=Path, default=DEFAULT_VISION_CACHE)
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY)
    parser.add_argument("--model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("--workers", type=int, default=28)
    parser.add_argument("--target-count", type=int, default=None, help="Optional cap on curated photos (default: every photo passing the quality gate)")
    parser.add_argument("--limit", type=int, default=None, help="Optional candidate limit for smoke test")
    parser.add_argument("--upgrade-originals", action="store_true", help="Upgrade thumb_640 photos to full_2048 via live USBCameraBridge")
    args = parser.parse_args()
    run_pipeline(args)


if __name__ == "__main__":
    main()

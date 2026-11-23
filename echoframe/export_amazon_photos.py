#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. EchoFrame; Section 7 attribution terms in ATTRIBUTION.md.
"""Export a clean, deduplicated Amazon Photos upload bundle for standalone Echo Show playback.

Reads `data/album/manifest.json` (plus `data/album/hidden.json` if present) and exports the
`Ambient Mix` (`in_default_frame: true`) slides into a flat, pure-JPEG upload directory
(`data/export/amazon_photos_ambient_mix/`):

- Landscapes (`aspect_ratio >= 1.15`) and pre-rendered `1600x960` portrait diptychs are
  copied losslessly (zero JPEG re-compression).
- Unpaired solo portraits and squares (`aspect_ratio < 1.15`) are matted onto a crisp
  `1600x960` (`16:10`) `#0b0e13` + `accent_rgb` gallery frame by default so Echo Show's
  native Amazon Photos wallpaper/Photo Frame never center-crops faces or adds stark bars.
- Every exported JPEG receives lossless EXIF `DateTimeOriginal` metadata from `creation_date`
  so Amazon Photos preserves real 2019–2026 capture dates instead of sorting by upload date.
- Tracks SHA-256 hashes in `data/export/amazon_photos_sync_state.json` (kept outside the
  upload folder so drag-and-drop uploads see 100% `.jpg` files), leaves unchanged files
  untouched, and populates `data/export/amazon_photos_delta/` with only newly added or
  upgraded JPEGs since the last `--mark-synced` checkpoint.

Safety: Makes zero network requests and never logs into or uploads to Amazon Photos without
explicit owner action.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from echoframe.extract_iphone_album import compose_gallery_matte, parse_iso_dt  # noqa: E402
from echoframe.serve_album import is_slide_hidden, load_hidden_ids  # noqa: E402
DEFAULT_ALBUM_DIR = Path.cwd() / "data" / "album"
DEFAULT_EXPORT_DIR = Path.cwd() / "data" / "export" / "amazon_photos_ambient_mix"
DEFAULT_DELTA_DIR = Path.cwd() / "data" / "export" / "amazon_photos_delta"
DEFAULT_SYNC_STATE = Path.cwd() / "data" / "export" / "amazon_photos_sync_state.json"

AMAZON_PHOTOS_FREE_TIER_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB
AMAZON_PHOTOS_ALBUMS_URL = "https://www.amazon.com/photos/albums"


def build_exif_bytes(creation_date: Optional[str], caption: str = "") -> bytes:
    """Build deterministic minimal EXIF payload with `DateTimeOriginal`."""
    dt = parse_iso_dt(creation_date)
    exif_dt = dt.strftime("%Y:%m:%d %H:%M:%S") if dt else None
    if not exif_dt and not caption:
        return b""
    exif = Image.Exif()
    if exif_dt:
        exif[0x0132] = exif_dt  # DateTime
        exif_ifd = exif.get_ifd(0x8769)
        exif_ifd[0x9003] = exif_dt  # DateTimeOriginal
        exif_ifd[0x9004] = exif_dt  # DateTimeDigitized
    if caption:
        ascii_desc = caption.encode("ascii", "ignore").decode("ascii").strip()
        if ascii_desc:
            exif[0x010E] = ascii_desc[:120]  # ImageDescription
    return exif.tobytes()


def inject_exif_jpeg_lossless(jpeg_bytes: bytes, exif_payload: bytes) -> bytes:
    """Insert or replace EXIF APP1 segment in `jpeg_bytes` without re-encoding image pixels."""
    if not exif_payload or len(jpeg_bytes) < 4 or jpeg_bytes[:2] != b"\xff\xd8":
        return jpeg_bytes

    app1_seg = b"\xff\xe1" + struct.pack(">H", len(exif_payload) + 2) + exif_payload

    # Walk top-level metadata markers before SOS (0xFFDA) to strip any prior Exif APP1
    # and keep JFIF APP0 (0xFFE0) first when present.
    pos = 2
    n = len(jpeg_bytes)
    prefix_segments: List[bytes] = []
    other_segments: List[bytes] = []

    while pos + 4 <= n and jpeg_bytes[pos] == 0xFF:
        marker = jpeg_bytes[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xDA, 0xD9):  # SOS or EOI
            break
        if marker == 0x00 or (0xD0 <= marker <= 0xD7) or marker == 0x01:
            break
        seg_len = struct.unpack(">H", jpeg_bytes[pos + 2 : pos + 4])[0]
        if seg_len < 2 or pos + 2 + seg_len > n:
            break
        seg_bytes = jpeg_bytes[pos : pos + 2 + seg_len]
        payload = jpeg_bytes[pos + 4 : pos + 2 + seg_len]
        pos += 2 + seg_len

        if marker == 0xE0:  # JFIF APP0 stays right after SOI
            prefix_segments.append(seg_bytes)
        elif marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            # Strip old Exif APP1 so we replace cleanly
            continue
        else:
            other_segments.append(seg_bytes)

    return b"\xff\xd8" + b"".join(prefix_segments) + app1_seg + b"".join(other_segments) + jpeg_bytes[pos:]


def render_single_matte_jpeg(
    src_path: Path,
    accent_rgb: List[int],
    canvas_size: Tuple[int, int] = (1600, 960),
    exif_payload: bytes = b"",
) -> bytes:
    """Matte a portrait/square single photo onto a 1600x960 (16:10) #0b0e13 gallery frame."""
    cw, ch = canvas_size
    with Image.open(src_path) as raw_im:
        im = ImageOps.exif_transpose(raw_im).convert("RGB")
        max_h = ch - 88  # 872px tall (44px top/bottom margin)
        max_w = cw - 160
        scale = min(max_w / float(max(1, im.width)), max_h / float(max(1, im.height)))
        card_w = max(1, int(round(im.width * scale)))
        card_h = max(1, int(round(im.height * scale)))
        tile = im.resize((card_w, card_h), Image.Resampling.LANCZOS)

    x_left = (cw - card_w) // 2
    y_top = (ch - card_h) // 2
    final_rgb = compose_gallery_matte(
        canvas_size,
        (accent_rgb or [28, 36, 48])[:3],
        [(tile, x_left, y_top)],
        blend=0.16,
        rx_scale=0.54,
        ry_scale=0.60,
    )
    buf = io.BytesIO()
    save_kwargs: Dict[str, Any] = {"format": "JPEG", "quality": 92, "subsampling": 0, "optimize": True}
    if exif_payload:
        save_kwargs["exif"] = exif_payload
    final_rgb.save(buf, **save_kwargs)
    return buf.getvalue()


def select_deduplicated_slides(
    manifest: Dict[str, Any],
    hidden_ids: Set[str],
    channel: str = "smart_frame",
) -> List[Dict[str, Any]]:
    """Select active slides for `channel`, removing hidden items and duplicate photo coverage."""
    all_slides: List[Dict[str, Any]] = manifest.get("slides") or []
    visible = [s for s in all_slides if not is_slide_hidden(s, hidden_ids)]

    if channel == "smart_frame":
        candidates = [s for s in visible if s.get("in_default_frame", True)]
    elif channel == "all":
        candidates = visible
    else:
        candidates = [s for s in visible if channel in (s.get("channels") or [])]

    # First collect all photo IDs covered by diptychs so no portrait is uploaded both inside a
    # diptych and again as a duplicate standalone shot in the same Amazon Photos album.
    diptych_member_ids: Set[str] = set()
    for s in candidates:
        if s.get("kind") == "diptych":
            for mid in s.get("member_ids") or []:
                diptych_member_ids.add(str(mid))

    seen_slide_ids: Set[str] = set()
    seen_rel_paths: Set[str] = set()
    deduped: List[Dict[str, Any]] = []

    for s in candidates:
        sid = str(s.get("id") or "").strip()
        rel_path = str(s.get("rel_path") or s.get("src", "").lstrip("/")).strip()
        if not sid or not rel_path or sid in seen_slide_ids or rel_path in seen_rel_paths:
            continue
        if s.get("kind") == "single":
            pid = str(s.get("photo_id") or sid)
            if pid in diptych_member_ids:
                continue
        seen_slide_ids.add(sid)
        seen_rel_paths.add(rel_path)
        deduped.append(s)

    return deduped


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json_atomic(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(path)


def export_amazon_photos_bundle(
    album_dir: Path = DEFAULT_ALBUM_DIR,
    export_dir: Path = DEFAULT_EXPORT_DIR,
    delta_dir: Path = DEFAULT_DELTA_DIR,
    sync_state_path: Path = DEFAULT_SYNC_STATE,
    channel: str = "smart_frame",
    matte_portraits: bool = True,
    mark_synced: bool = False,
) -> Dict[str, Any]:
    """Build or update the deduplicated Amazon Photos upload folder and delta staging directory."""
    manifest_path = album_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Curated manifest not found at {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hidden_ids = load_hidden_ids(album_dir)
    slides = select_deduplicated_slides(manifest, hidden_ids=hidden_ids, channel=channel)

    prev_state: Dict[str, Any] = {}
    if sync_state_path.is_file():
        try:
            prev_state = json.loads(sync_state_path.read_text(encoding="utf-8"))
        except Exception:
            prev_state = {}

    prev_exported: Dict[str, Dict[str, Any]] = prev_state.get("exported_files") or {}
    last_synced: Dict[str, str] = dict(prev_state.get("amazon_synced_sha256") or {})

    export_dir.mkdir(parents=True, exist_ok=True)

    # Remove any accidental non-JPEG files (e.g., .DS_Store) so folder drag-and-drop stays 100% clean
    for item in export_dir.iterdir():
        if item.is_file() and item.suffix.lower() != ".jpg":
            item.unlink(missing_ok=True)

    added: List[str] = []
    updated: List[str] = []
    unchanged: List[str] = []
    matted_count = 0
    diptych_count = 0
    landscape_count = 0

    next_exported: Dict[str, Dict[str, Any]] = {}
    expected_filenames: Set[str] = set()

    for slide in slides:
        sid = slide["id"]
        out_name = f"{sid}.jpg"
        expected_filenames.add(out_name)

        rel_path = slide.get("rel_path") or slide["src"].lstrip("/")
        src_file = album_dir / rel_path
        if not src_file.is_file():
            raise FileNotFoundError(f"Slide image missing on disk: {src_file}")

        src_sha = sha256_file(src_file)
        aspect = float(slide.get("aspect_ratio") or 1.5)
        kind = slide.get("kind") or "single"
        needs_matte = bool(matte_portraits and kind == "single" and aspect < 1.15)
        render_mode = "matted_16x10" if needs_matte else ("diptych_16x10" if kind == "diptych" else "landscape_lossless")

        if needs_matte:
            matted_count += 1
        elif kind == "diptych":
            diptych_count += 1
        else:
            landscape_count += 1

        dst_file = export_dir / out_name
        prev_rec = prev_exported.get(out_name)

        # Fast-path idempotency: if source SHA-256, render mode, creation_date, and output file match, keep untouched!
        if (
            prev_rec
            and dst_file.is_file()
            and prev_rec.get("source_sha256") == src_sha
            and prev_rec.get("render_mode") == render_mode
            and prev_rec.get("creation_date") == (slide.get("creation_date") or "")
            and sha256_file(dst_file) == prev_rec.get("export_sha256")
        ):
            unchanged.append(out_name)
            next_exported[out_name] = prev_rec
            continue

        exif_bytes = build_exif_bytes(
            slide.get("creation_date"),
            caption=str(slide.get("display_caption") or slide.get("label") or ""),
        )
        if needs_matte:
            out_bytes = render_single_matte_jpeg(
                src_file,
                accent_rgb=slide.get("accent_rgb") or [28, 36, 48],
                canvas_size=(1600, 960),
                exif_payload=exif_bytes,
            )
        else:
            raw_bytes = src_file.read_bytes()
            out_bytes = inject_exif_jpeg_lossless(raw_bytes, exif_bytes)

        out_sha = hashlib.sha256(out_bytes).hexdigest()
        if dst_file.is_file():
            if sha256_file(dst_file) == out_sha:
                unchanged.append(out_name)
            else:
                dst_file.write_bytes(out_bytes)
                updated.append(out_name)
        else:
            dst_file.write_bytes(out_bytes)
            added.append(out_name)

        next_exported[out_name] = {
            "slide_id": sid,
            "kind": kind,
            "render_mode": render_mode,
            "resolution_tier": slide.get("resolution_tier") or "unknown",
            "creation_date": slide.get("creation_date") or "",
            "display_caption": slide.get("display_caption") or "",
            "source_rel_path": rel_path,
            "source_sha256": src_sha,
            "export_sha256": out_sha,
            "bytes": len(out_bytes),
        }

    # Prune stale/hidden files from export_dir
    removed: List[str] = []
    for existing in sorted(export_dir.glob("*.jpg")):
        if existing.name not in expected_filenames:
            existing.unlink(missing_ok=True)
            removed.append(existing.name)

    if mark_synced:
        last_synced = {fname: rec["export_sha256"] for fname, rec in next_exported.items()}

    # Rebuild delta_dir containing ONLY files not yet confirmed synced at their current SHA-256
    if delta_dir.exists():
        shutil.rmtree(delta_dir)
    delta_dir.mkdir(parents=True, exist_ok=True)

    pending_upload: List[str] = []
    for fname, rec in sorted(next_exported.items()):
        if last_synced.get(fname) != rec["export_sha256"]:
            shutil.copy2(export_dir / fname, delta_dir / fname)
            pending_upload.append(fname)

    removed_since_sync = sorted(fname for fname in last_synced if fname not in next_exported)
    total_bytes = sum(rec["bytes"] for rec in next_exported.values())
    free_tier_pct = round((total_bytes / float(AMAZON_PHOTOS_FREE_TIER_BYTES)) * 100.0, 2)

    state_doc = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "matte_portraits": matte_portraits,
        "album_name_suggestion": "Ambient Mix",
        "summary": {
            "total_slides": len(next_exported),
            "landscape_singles": landscape_count,
            "diptych_16x10_slides": diptych_count,
            "matted_16x10_singles": matted_count,
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "amazon_free_5gb_percent": free_tier_pct,
            "added_this_run": len(added),
            "updated_this_run": len(updated),
            "removed_this_run": len(removed),
            "unchanged_this_run": len(unchanged),
            "pending_amazon_upload_count": len(pending_upload),
            "removed_from_amazon_album_count": len(removed_since_sync),
        },
        "pending_amazon_upload_files": pending_upload,
        "removed_from_amazon_album_files": removed_since_sync,
        "amazon_synced_sha256": last_synced,
        "exported_files": next_exported,
    }

    write_json_atomic(sync_state_path, state_doc)

    return state_doc["summary"] | {
        "export_dir": str(export_dir),
        "delta_dir": str(delta_dir),
        "sync_state_path": str(sync_state_path),
        "removed_from_amazon_album_files": removed_since_sync,
    }


def main(argv: Optional[List[str]] = None, prog: Optional[str] = None) -> None:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("--album-dir", type=Path, default=DEFAULT_ALBUM_DIR)
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--delta-dir", type=Path, default=DEFAULT_DELTA_DIR)
    parser.add_argument("--sync-state", type=Path, default=DEFAULT_SYNC_STATE)
    parser.add_argument("--channel", default="smart_frame", help="Manifest channel to export (default: smart_frame / Ambient Mix)")
    parser.add_argument(
        "--no-matte-portraits",
        action="store_true",
        help="Export raw portrait/square JPEGs instead of pre-matting onto 1600x960 gallery frames",
    )
    parser.add_argument(
        "--mark-synced",
        action="store_true",
        help="Record all currently exported slide SHA-256 hashes as uploaded to Amazon Photos (clears delta folder)",
    )
    parser.add_argument(
        "--reveal",
        action="store_true",
        help="Reveal the exported folder in macOS Finder",
    )
    parser.add_argument(
        "--open-amazon-photos",
        action="store_true",
        help="Explicit opt-in: open Amazon Photos Albums in your default browser and reveal the upload folder",
    )
    args = parser.parse_args(argv)

    summary = export_amazon_photos_bundle(
        album_dir=args.album_dir,
        export_dir=args.export_dir,
        delta_dir=args.delta_dir,
        sync_state_path=args.sync_state,
        channel=args.channel,
        matte_portraits=not args.no_matte_portraits,
        mark_synced=args.mark_synced,
    )

    print(
        f"[amazon-export] {summary['total_slides']} landscape-ready slides -> {summary['export_dir']} "
        f"({summary['total_mb']} MB, {summary['amazon_free_5gb_percent']}% of 5 GB free tier)",
        flush=True,
    )
    print(
        f"[amazon-export] Breakdown: {summary['landscape_singles']} landscapes + "
        f"{summary['diptych_16x10_slides']} diptychs (1600x960) + "
        f"{summary['matted_16x10_singles']} matted portraits/squares (1600x960)",
        flush=True,
    )
    print(
        f"[amazon-export] Diff this run: +{summary['added_this_run']} added, "
        f"~{summary['updated_this_run']} updated, -{summary['removed_this_run']} removed, "
        f"={summary['unchanged_this_run']} unchanged",
        flush=True,
    )
    print(
        f"[amazon-export] Pending upload delta: {summary['pending_amazon_upload_count']} JPEGs in {summary['delta_dir']}",
        flush=True,
    )
    if summary["removed_from_amazon_album_files"]:
        print(
            f"[amazon-export] Hidden/removed since last sync ({len(summary['removed_from_amazon_album_files'])}): "
            + ", ".join(summary["removed_from_amazon_album_files"][:12]),
            flush=True,
        )

    if args.reveal or args.open_amazon_photos:
        target_reveal = Path(summary["delta_dir"]) if summary["pending_amazon_upload_count"] > 0 else Path(summary["export_dir"])
        subprocess.run(["open", str(target_reveal)], check=False)
    if args.open_amazon_photos:
        subprocess.run(["open", AMAZON_PHOTOS_ALBUMS_URL], check=False)


if __name__ == "__main__":
    main()

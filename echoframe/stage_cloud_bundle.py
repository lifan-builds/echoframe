#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. EchoFrame; Section 7 attribution terms in ATTRIBUTION.md.
"""Stage (and optionally push) the private Cloudflare Worker + R2 bundle.

Prepares `data/export/cloud_stage/` from `data/album/`:
- `manifest.json`
- `embeddings_meta.json` + `embeddings.f16.bin` (compact little-endian float16 vectors
  from `album_embeddings.npz`, ~1.9 MB total)
- `photos/*.jpg` and `diptychs/*.jpg`
- `state/hidden.json` (when local hidden slides exist)

Also manages a private 256-bit `FRAME_ACCESS_KEY` at
`~/.local/share/alexa-photos/cloud_access_key` (mode `0600`, outside git).

Safety: Never contacts Cloudflare or uploads anything externally unless `--push` is
explicitly passed by the owner after authorizing deployment.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from echoframe.export_amazon_photos import sha256_file, write_json_atomic  # noqa: E402
DEFAULT_ALBUM_DIR = Path.cwd() / "data" / "album"
DEFAULT_STAGE_DIR = Path.cwd() / "data" / "export" / "cloud_stage"
DEFAULT_SYNC_STATE = Path.cwd() / "data" / "export" / "cloud_sync_state.json"
DEFAULT_BUCKET_NAME = "alexa-photos-private"
DEFAULT_KEY_FILE = Path.home() / ".local" / "share" / "alexa-photos" / "cloud_access_key"

CLOUDFLARE_R2_FREE_TIER_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_private_access_key(key_path: Path = DEFAULT_KEY_FILE, rotate: bool = False) -> str:
    """Load or generate a 256-bit URL-safe `FRAME_ACCESS_KEY` stored with mode 0600."""
    if key_path.is_file() and not rotate:
        existing = key_path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            os.chmod(key_path, 0o600)
            return existing

    key_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(key_path.parent, 0o700)
    token = secrets.token_urlsafe(32)  # 256 bits of entropy (~43 URL-safe chars)
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(key_path, 0o600)
    return token


def pack_embeddings_float16(npz_path: Path) -> Tuple[Dict[str, Any], bytes]:
    """Convert `album_embeddings.npz` into `(embeddings_meta_dict, float16_le_bytes)`."""
    loaded = np.load(npz_path, allow_pickle=False)
    ids = [str(x) for x in loaded["ids"].tolist()]
    vecs = loaded["vectors"].astype("<f2")  # Explicit little-endian IEEE-754 float16
    rows, dim = int(vecs.shape[0]), int(vecs.shape[1]) if vecs.ndim == 2 else 0
    meta = {
        "version": 1,
        "count": rows,
        "dim": dim,
        "dtype": "float16_le",
        "ids": ids,
    }
    return meta, vecs.tobytes(order="C")


def link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def mime_for_r2_key(r2_key: str) -> str:
    low = r2_key.lower()
    if low.endswith(".json"):
        return "application/json"
    if low.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if low.endswith(".png"):
        return "image/png"
    if low.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"


def stage_cloud_bundle(
    album_dir: Path = DEFAULT_ALBUM_DIR,
    stage_dir: Path = DEFAULT_STAGE_DIR,
    sync_state_path: Path = DEFAULT_SYNC_STATE,
    bucket_name: str = DEFAULT_BUCKET_NAME,
) -> Dict[str, Any]:
    """Stage R2 objects locally and compute SHA-256 delta against last pushed cloud state."""
    manifest_path = album_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Curated manifest not found at {manifest_path}")

    prev_state: Dict[str, Any] = {}
    if sync_state_path.is_file():
        try:
            prev_state = json.loads(sync_state_path.read_text(encoding="utf-8"))
        except Exception:
            prev_state = {}

    r2_synced: Dict[str, str] = dict(prev_state.get("r2_synced_sha256") or {})

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    staged_objects: Dict[str, Dict[str, Any]] = {}

    def register_bytes(r2_key: str, data: bytes) -> None:
        out_file = stage_dir / r2_key
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(data)
        staged_objects[r2_key] = {
            "sha256": sha256_bytes(data),
            "bytes": len(data),
            "content_type": mime_for_r2_key(r2_key),
        }

    def register_media_file(r2_key: str, src_file: Path) -> None:
        out_file = stage_dir / r2_key
        link_or_copy(src_file, out_file)
        staged_objects[r2_key] = {
            "sha256": sha256_file(src_file),
            "bytes": src_file.stat().st_size,
            "content_type": mime_for_r2_key(r2_key),
        }

    # 1. Stage manifest.json
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw.decode("utf-8"))
    register_bytes("manifest.json", manifest_raw)

    # 2. Stage float16 semantic embeddings if present
    npz_path = album_dir / "album_embeddings.npz"
    if npz_path.is_file():
        emb_meta, emb_bin = pack_embeddings_float16(npz_path)
        register_bytes("embeddings_meta.json", json.dumps(emb_meta, separators=(",", ":")).encode("utf-8"))
        register_bytes("embeddings.f16.bin", emb_bin)

    # 3. Stage hidden.json if present
    hidden_path = album_dir / "hidden.json"
    if hidden_path.is_file():
        register_bytes("state/hidden.json", hidden_path.read_bytes())

    # 4. Stage all referenced photos and diptychs
    seen_media: Set[str] = set()
    for p in manifest.get("photos") or []:
        rel = str(p.get("rel_path") or p.get("src", "").lstrip("/")).strip()
        if rel and rel not in seen_media:
            src = album_dir / rel
            if src.is_file():
                register_media_file(rel, src)
                seen_media.add(rel)

    for s in manifest.get("slides") or []:
        rel = str(s.get("rel_path") or s.get("src", "").lstrip("/")).strip()
        if rel and rel not in seen_media:
            src = album_dir / rel
            if src.is_file():
                register_media_file(rel, src)
                seen_media.add(rel)

    pending_keys = sorted(
        k for k, info in staged_objects.items() if r2_synced.get(k) != info["sha256"]
    )
    stale_keys = sorted(k for k in r2_synced if k not in staged_objects)

    total_bytes = sum(info["bytes"] for info in staged_objects.values())
    free_pct = round((total_bytes / float(CLOUDFLARE_R2_FREE_TIER_BYTES)) * 100.0, 2)

    state_doc = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "bucket_name": bucket_name,
        "summary": {
            "total_objects": len(staged_objects),
            "media_objects": len(seen_media),
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "r2_free_10gb_percent": free_pct,
            "pending_r2_upload_count": len(pending_keys),
            "stale_r2_delete_count": len(stale_keys),
        },
        "pending_r2_keys": pending_keys,
        "stale_r2_keys": stale_keys,
        "r2_synced_sha256": r2_synced,
        "staged_objects": staged_objects,
    }

    write_json_atomic(sync_state_path, state_doc)

    return state_doc["summary"] | {
        "stage_dir": str(stage_dir),
        "sync_state_path": str(sync_state_path),
        "bucket_name": bucket_name,
    }


def find_wrangler_cmd() -> Optional[List[str]]:
    """Locate an installed `wrangler` binary without downloading anything automatically."""
    direct = shutil.which("wrangler")
    if direct:
        return [direct]
    local_bin = ROOT / "node_modules" / ".bin" / "wrangler"
    if local_bin.is_file():
        return [str(local_bin)]
    return None


def push_staged_bundle_to_cloudflare(
    stage_dir: Path = DEFAULT_STAGE_DIR,
    sync_state_path: Path = DEFAULT_SYNC_STATE,
    bucket_name: str = DEFAULT_BUCKET_NAME,
    deploy_worker: bool = True,
    workers: int = 8,
) -> Dict[str, Any]:
    """Push changed R2 objects and optionally deploy the Worker using installed `wrangler`."""
    wrangler = find_wrangler_cmd()
    if wrangler is None:
        raise RuntimeError(
            "Wrangler CLI is not installed on PATH or ./node_modules/.bin/wrangler.\n"
            "Install it once with: npm install --save-dev wrangler\n"
            "Then authenticate with: npx wrangler login"
        )

    whoami = subprocess.run(wrangler + ["whoami"], cwd=str(ROOT), capture_output=True, text=True, check=False)
    if whoami.returncode != 0:
        raise RuntimeError(
            "Wrangler is not authenticated to Cloudflare yet. Run `npx wrangler login` first.\n"
            f"Details: {(whoami.stderr or whoami.stdout).strip()[:300]}"
        )

    if not sync_state_path.is_file():
        raise FileNotFoundError(f"Sync state missing at {sync_state_path}; run stage_cloud_bundle first.")

    state_doc = json.loads(sync_state_path.read_text(encoding="utf-8"))
    staged_objects: Dict[str, Dict[str, Any]] = state_doc.get("staged_objects") or {}
    r2_synced: Dict[str, str] = dict(state_doc.get("r2_synced_sha256") or {})
    pending_keys: List[str] = state_doc.get("pending_r2_keys") or []
    stale_keys: List[str] = state_doc.get("stale_r2_keys") or []

    def put_one(r2_key: str) -> str:
        info = staged_objects[r2_key]
        file_path = stage_dir / r2_key
        cmd = wrangler + [
            "r2",
            "object",
            "put",
            f"{bucket_name}/{r2_key}",
            "--file",
            str(file_path),
            "--content-type",
            info["content_type"],
            "--remote",
        ]
        res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"Failed uploading {r2_key}: {(res.stderr or res.stdout).strip()[:240]}")
        return r2_key

    uploaded = 0
    if pending_keys:
        print(f"[cloud-push] Uploading {len(pending_keys)} changed objects to private R2 bucket '{bucket_name}'...", flush=True)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = {pool.submit(put_one, k): k for k in pending_keys}
            for fut in as_completed(futs):
                done_key = fut.result()
                r2_synced[done_key] = staged_objects[done_key]["sha256"]
                uploaded += 1

    deleted = 0
    for stale_key in stale_keys:
        cmd = wrangler + ["r2", "object", "delete", f"{bucket_name}/{stale_key}", "--remote"]
        res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)
        if res.returncode == 0:
            r2_synced.pop(stale_key, None)
            deleted += 1

    state_doc["r2_synced_sha256"] = r2_synced
    state_doc["pending_r2_keys"] = [
        k for k, info in staged_objects.items() if r2_synced.get(k) != info["sha256"]
    ]
    state_doc["stale_r2_keys"] = [k for k in r2_synced if k not in staged_objects]
    state_doc["summary"]["pending_r2_upload_count"] = len(state_doc["pending_r2_keys"])
    state_doc["summary"]["stale_r2_delete_count"] = len(state_doc["stale_r2_keys"])
    state_doc["last_pushed_at"] = datetime.now(timezone.utc).isoformat()
    write_json_atomic(sync_state_path, state_doc)

    worker_url = ""
    if deploy_worker:
        print("[cloud-push] Deploying Worker & static web assets (`wrangler deploy`)...", flush=True)
        dep = subprocess.run(wrangler + ["deploy"], cwd=str(ROOT), capture_output=True, text=True, check=False)
        if dep.returncode != 0:
            raise RuntimeError(f"wrangler deploy failed: {(dep.stderr or dep.stdout).strip()[:320]}")
        for line in (dep.stdout or "").splitlines():
            if "https://" in line and ".workers.dev" in line:
                worker_url = line.strip()

    return {
        "uploaded_objects": uploaded,
        "deleted_objects": deleted,
        "worker_deployed": deploy_worker,
        "worker_output_line": worker_url,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--album-dir", type=Path, default=DEFAULT_ALBUM_DIR)
    parser.add_argument("--stage-dir", type=Path, default=DEFAULT_STAGE_DIR)
    parser.add_argument("--sync-state", type=Path, default=DEFAULT_SYNC_STATE)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET_NAME)
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--generate-key", action="store_true", help="Ensure a 256-bit FRAME_ACCESS_KEY exists in ~/.local/share/alexa-photos/cloud_access_key")
    parser.add_argument("--rotate-key", action="store_true", help="Generate a fresh 256-bit FRAME_ACCESS_KEY")
    parser.add_argument("--push", action="store_true", help="Explicit opt-in: push staged delta to private R2 and deploy Worker")
    parser.add_argument("--r2-only", action="store_true", help="With --push, sync R2 objects only and skip `wrangler deploy`")
    args = parser.parse_args()

    if args.generate_key or args.rotate_key:
        ensure_private_access_key(args.key_file, rotate=args.rotate_key)
        print(f"[cloud-key] Private 256-bit FRAME_ACCESS_KEY ready at {args.key_file} (mode 0600)", flush=True)

    summary = stage_cloud_bundle(
        album_dir=args.album_dir,
        stage_dir=args.stage_dir,
        sync_state_path=args.sync_state,
        bucket_name=args.bucket,
    )
    print(
        f"[cloud-stage] Staged {summary['total_objects']} R2 objects ({summary['media_objects']} JPEGs + manifest + f16 embeddings) "
        f"-> {summary['stage_dir']} ({summary['total_mb']} MB, {summary['r2_free_10gb_percent']}% of 10 GB free tier)",
        flush=True,
    )
    print(
        f"[cloud-stage] Incremental R2 delta: {summary['pending_r2_upload_count']} objects to upload, "
        f"{summary['stale_r2_delete_count']} stale objects to prune",
        flush=True,
    )

    if args.push:
        push_res = push_staged_bundle_to_cloudflare(
            stage_dir=args.stage_dir,
            sync_state_path=args.sync_state,
            bucket_name=args.bucket,
            deploy_worker=not args.r2_only,
        )
        print(f"[cloud-push] Done: {json.dumps(push_res)}", flush=True)


if __name__ == "__main__":
    main()

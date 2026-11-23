#!/usr/bin/env python3
"""Automated background sync of exported `Ambient Mix` JPEGs into Amazon Photos via `fmu`.

Uses `faster-mac-use` (`mode="extension"`, `🤖 Agent` Chrome tab group, zero focus/cursor theft)
to upload any pending JPEGs in `data/export/amazon_photos_delta/` directly into the dedicated
`Ambient Mix` album in Amazon Photos and record the `--mark-synced` checkpoint locally.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from echoframe.export_amazon_photos import (  # noqa: E402
    DEFAULT_ALBUM_DIR,
    DEFAULT_DELTA_DIR,
    DEFAULT_EXPORT_DIR,
    DEFAULT_SYNC_STATE,
    export_amazon_photos_bundle,
    write_json_atomic,
)

PROFILE_PATH = Path.cwd() / "private" / "profile.json"
_PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8")) if PROFILE_PATH.is_file() else {}
DEFAULT_AMAZON_ALBUM_ID = os.environ.get("AMAZON_ALBUM_ID") or _PROFILE.get("amazon_album_id") or ""
DEFAULT_ALBUM_TITLE = os.environ.get("AMAZON_ALBUM_TITLE") or _PROFILE.get("amazon_album_title") or "Ambient Mix"


def resolve_fmu_bin(explicit: Optional[Path] = None) -> Path:
    if explicit and explicit.is_file():
        return explicit
    found = shutil.which("fmu")
    if found:
        return Path(found)
    raise FileNotFoundError("fmu CLI not found on PATH (install faster-mac-use)")


def run_fmu_json(fmu_bin: Path, payload: Dict[str, Any], timeout: int = 45) -> str:
    proc = subprocess.run(
        [str(fmu_bin), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"fmu failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


def eval_in_chrome_tab(fmu_bin: Path, url: str, js_expr: str, timeout: int = 45) -> str:
    code = (
        f'const tab = await cua.createBrowserTab("chrome", {json.dumps(url)}, '
        '{"sessionName": "🤖 Agent", "reuseExisting": true}); '
        f"return await tab.evaluate({json.dumps(js_expr)});"
    )
    return run_fmu_json(fmu_bin, {"mode": "extension", "code": code}, timeout=timeout).strip()


def sync_delta_to_amazon_photos(
    album_dir: Path = DEFAULT_ALBUM_DIR,
    export_dir: Path = DEFAULT_EXPORT_DIR,
    delta_dir: Path = DEFAULT_DELTA_DIR,
    sync_state_path: Path = DEFAULT_SYNC_STATE,
    amazon_album_id: str = DEFAULT_AMAZON_ALBUM_ID,
    album_title: str = "Ambient Mix",
    fmu_bin: Optional[Path] = None,
    poll_timeout_sec: int = 420,
    batch_size: int = 80,
) -> Dict[str, Any]:
    """Export current delta, upload pending JPEGs in batches into Amazon Photos `Ambient Mix`, and mark synced."""
    summary = export_amazon_photos_bundle(
        album_dir=album_dir,
        export_dir=export_dir,
        delta_dir=delta_dir,
        sync_state_path=sync_state_path,
        mark_synced=False,
    )

    state_doc: Dict[str, Any] = {}
    if sync_state_path.is_file():
        state_doc = json.loads(sync_state_path.read_text(encoding="utf-8"))
    amazon_album_id = amazon_album_id or state_doc.get("amazon_album_id") or DEFAULT_AMAZON_ALBUM_ID
    if not amazon_album_id:
        raise ValueError("Pass --amazon-album-id <ID> (from https://www.amazon.com/photos/album/<ID>) or set AMAZON_ALBUM_ID.")

    pending_paths: List[Path] = sorted(p.resolve() for p in delta_dir.glob("*.jpg"))
    if not pending_paths:
        print("[amazon-sync] Already up to date (0 pending JPEGs).", flush=True)
        return {"uploaded": 0, "album_id": amazon_album_id, "status": "up_to_date", "export_summary": summary}

    fmu = resolve_fmu_bin(fmu_bin)
    album_url = f"https://www.amazon.com/photos/album/{amazon_album_id}"
    total = len(pending_paths)
    chunks = [pending_paths[i : i + max(1, batch_size)] for i in range(0, total, max(1, batch_size))]
    print(f"[amazon-sync] Uploading {total} delta JPEGs in {len(chunks)} batch(es) to {album_url}...", flush=True)

    check_js = """JSON.stringify({
      status: Array.from(document.querySelectorAll('[role="status"]')).map(e => e.innerText.trim()).join(' | '),
      ready: Array.from(document.querySelectorAll('button')).some(b => b.innerText.trim() === 'Add to album' && !b.disabled)
    })"""
    attach_code = f"""
const tab = await cua.createBrowserTab("chrome", {json.dumps(album_url)}, {{"sessionName": "🤖 Agent", "reuseExisting": true}});
await tab.click("Add to album");
await tab.wait(1800);
const picked = await tab.evaluate(`(() => {{
  const title = {json.dumps(album_title)};
  const btns = Array.from(document.querySelectorAll('button'));
  const hit = btns.find(b => b.innerText && b.innerText.trim().startsWith(title) && b.offsetParent !== null && b.innerText.trim() !== title);
  const fallback = btns.filter(b => b.innerText && b.innerText.trim().startsWith(title)).pop();
  const chosen = hit || fallback;
  if (chosen) {{ chosen.click(); return chosen.innerText.trim(); }}
  return null;
}})()`);
await tab.wait(5000);
return {{ picked }};
"""

    uploaded_so_far = 0
    for b_idx, chunk in enumerate(chunks, start=1):
        chunk_files = [str(p) for p in chunk]
        print(f"  [amazon-sync] Batch {b_idx}/{len(chunks)} ({len(chunk_files)} JPEGs)...", flush=True)
        if b_idx > 1:
            run_fmu_json(
                fmu,
                {
                    "mode": "extension",
                    "code": (
                        f'const tab = await cua.createBrowserTab("chrome", {json.dumps(album_url)}, '
                        '{"sessionName": "🤖 Agent", "reuseExisting": true}); '
                        "await tab.evaluate('location.reload()'); await tab.wait(3000); return 'reloaded';"
                    ),
                },
                timeout=30,
            )

        open_and_upload = {
            "mode": "extension",
            "url": album_url,
            "reuseExisting": True,
            "steps": [
                {"op": "wait", "ms": 2200},
                {"op": "upload", "target": "input.uploader-file-selector", "files": chunk_files},
                {"op": "wait", "ms": 2500},
            ],
        }
        run_fmu_json(fmu, open_and_upload, timeout=75)

        deadline = time.time() + poll_timeout_sec
        completed = False
        while time.time() < deadline:
            out = eval_in_chrome_tab(fmu, album_url, check_js, timeout=30)
            if '"ready":true' in out or '"ready": true' in out:
                completed = True
                break
            time.sleep(4.0)

        if not completed:
            raise TimeoutError(f"Timed out after {poll_timeout_sec}s waiting for batch {b_idx}/{len(chunks)} upload completion.")

        run_fmu_json(fmu, {"mode": "extension", "code": attach_code}, timeout=45)

        # Checkpoint completed batch into sync_state_path so retries never re-upload finished batches
        if sync_state_path.is_file():
            cur_doc = json.loads(sync_state_path.read_text(encoding="utf-8"))
            exp_map = cur_doc.get("exported_files") or {}
            synced_map = cur_doc.setdefault("amazon_synced_sha256", {})
            for p in chunk:
                rec = exp_map.get(p.name)
                if rec and rec.get("export_sha256"):
                    synced_map[p.name] = rec["export_sha256"]
                p.unlink(missing_ok=True)
            write_json_atomic(sync_state_path, cur_doc)

        uploaded_so_far += len(chunk_files)
        print(f"  [amazon-sync] Attached {uploaded_so_far}/{total} JPEGs to '{album_title}'", flush=True)

    final_summary = export_amazon_photos_bundle(
        album_dir=album_dir,
        export_dir=export_dir,
        delta_dir=delta_dir,
        sync_state_path=sync_state_path,
        mark_synced=True,
    )
    if sync_state_path.is_file():
        updated_doc = json.loads(sync_state_path.read_text(encoding="utf-8"))
        updated_doc["amazon_album_id"] = amazon_album_id
        updated_doc["amazon_album_url"] = album_url
        write_json_atomic(sync_state_path, updated_doc)

    print(f"[amazon-sync] Synced {total} slides into '{album_title}' ({album_url}).", flush=True)
    return {
        "uploaded": total,
        "album_id": amazon_album_id,
        "album_url": album_url,
        "status": "synced",
        "export_summary": final_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--album-dir", type=Path, default=DEFAULT_ALBUM_DIR)
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--delta-dir", type=Path, default=DEFAULT_DELTA_DIR)
    parser.add_argument("--sync-state", type=Path, default=DEFAULT_SYNC_STATE)
    parser.add_argument("--amazon-album-id", default=DEFAULT_AMAZON_ALBUM_ID)
    parser.add_argument("--album-title", default=DEFAULT_ALBUM_TITLE)
    args = parser.parse_args()

    res = sync_delta_to_amazon_photos(
        album_dir=args.album_dir,
        export_dir=args.export_dir,
        delta_dir=args.delta_dir,
        sync_state_path=args.sync_state,
        amazon_album_id=args.amazon_album_id,
        album_title=args.album_title,
    )
    print(json.dumps(res, indent=2), flush=True)


if __name__ == "__main__":
    main()

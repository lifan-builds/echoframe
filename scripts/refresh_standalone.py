#!/usr/bin/env python3
"""One-command occasional Mac refresh for standalone Echo Show & multi-device playback.

Run this single command whenever you plug in your iPhone and open your MacBook, then
unplug your iPhone and close your Mac again:

    python3 scripts/refresh_standalone.py

Pipeline:
1. Upgrade any remaining `thumb_640` curated photos to `2048px` masters via live
   `USBCameraBridge` (or run full OpenAI re-curation + embeddings if `--recurate` is set)
   and re-render any affected/missing `1600x960` portrait diptychs.
2. Export the deduplicated `206`-slide `Ambient Mix` upload bundle (`data/export/amazon_photos_ambient_mix/`)
   and incremental upload delta (`data/export/amazon_photos_delta/`) for native Echo Show
   Amazon Photos playback.
3. Stage the private Cloudflare Worker + R2 bundle (`data/export/cloud_stage/`) with compact
   `float16` semantic embeddings, and—only when `--push-cloud` is explicitly passed—sync
   changed SHA-256 objects to private R2 so all tablets, TVs, phones, and Silk browsers
   update automatically while your Mac sleeps.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_amazon_photos import (  # noqa: E402
    AMAZON_PHOTOS_ALBUMS_URL,
    DEFAULT_DELTA_DIR,
    DEFAULT_EXPORT_DIR,
    DEFAULT_SYNC_STATE as DEFAULT_AMAZON_SYNC_STATE,
    export_amazon_photos_bundle,
)
from scripts.extract_iphone_album import (  # noqa: E402
    CURATOR_REPO,
    DEFAULT_CATALOG,
    DEFAULT_GATEWAY,
    DEFAULT_OUT_DIR,
    DEFAULT_USB_DAEMON,
    DEFAULT_USB_ORIGINALS,
    DEFAULT_VISION_CACHE,
    DEFAULT_VISION_MODEL,
    rerender_manifest_diptychs,
    run_pipeline,
    upgrade_originals_in_place,
)
from scripts.stage_cloud_bundle import (  # noqa: E402
    DEFAULT_BUCKET_NAME,
    DEFAULT_KEY_FILE,
    DEFAULT_STAGE_DIR,
    DEFAULT_SYNC_STATE as DEFAULT_CLOUD_SYNC_STATE,
    ensure_private_access_key,
    push_staged_bundle_to_cloudflare,
    stage_cloud_bundle,
)
from scripts.sync_amazon_photos import sync_delta_to_amazon_photos  # noqa: E402
from scripts.deploy_webos_tv import (  # noqa: E402
    DEFAULT_STAGE_DIR as DEFAULT_TV_STAGE_DIR,
    deploy_to_tv,
    export_tv_bundle,
    resolve_tv_ip,
)


def refresh_catalog(usb_daemon_dir: Path, catalog_path: Path, gateway_url: str) -> Dict[str, Any]:
    """Placeholder hook for custom external catalog pipelines (use `--source-dir` on `extract_iphone_album.py` for direct folders)."""
    raise RuntimeError("Pass `--source-dir <folder>` to `scripts/extract_iphone_album.py` for standalone folder ingestion.")


def verify_or_rebuild_diptychs(album_dir: Path, force_rebuild: bool = False) -> int:
    """Ensure every diptych slide in `manifest.json` exists on disk (or force re-render all)."""
    manifest_path = album_dir / "manifest.json"
    if not manifest_path.is_file():
        return 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    photo_by_id = {p["id"]: p for p in (manifest.get("photos") or [])}
    return rerender_manifest_diptychs(album_dir, photo_by_id, manifest.get("slides") or [], force_rebuild=force_rebuild)


def run_refresh(
    album_dir: Path = DEFAULT_OUT_DIR,
    usb_originals_dir: Path = DEFAULT_USB_ORIGINALS,
    usb_daemon_dir: Path = DEFAULT_USB_DAEMON,
    catalog_path: Path = DEFAULT_CATALOG,
    vision_cache_path: Path = DEFAULT_VISION_CACHE,
    gateway_url: str = DEFAULT_GATEWAY,
    recurate: bool = False,
    refresh_iphone_catalog: bool = False,
    rebuild_diptychs: bool = False,
    amazon_export_dir: Path = DEFAULT_EXPORT_DIR,
    amazon_delta_dir: Path = DEFAULT_DELTA_DIR,
    amazon_sync_state: Path = DEFAULT_AMAZON_SYNC_STATE,
    mark_amazon_synced: bool = False,
    sync_amazon: bool = False,
    cloud_stage_dir: Path = DEFAULT_STAGE_DIR,
    cloud_sync_state: Path = DEFAULT_CLOUD_SYNC_STATE,
    cloud_bucket: str = DEFAULT_BUCKET_NAME,
    key_file: Path = DEFAULT_KEY_FILE,
    stage_cloud: bool = False,
    push_cloud: bool = False,
    deploy_worker: bool = False,
    sync_tv: bool = False,
    tv_ip: str = "",
    tv_stage_dir: Path = DEFAULT_TV_STAGE_DIR,
) -> Dict[str, Any]:
    """Execute the end-to-end Mac refresh workflow safely and idempotently."""
    upgraded_count = 0
    if refresh_iphone_catalog:
        print(f"[catalog] {json.dumps(refresh_catalog(usb_daemon_dir, catalog_path, gateway_url))}", flush=True)
    if recurate or refresh_iphone_catalog:
        ns = argparse.Namespace(
            source_dir=None,
            catalog=catalog_path,
            usb_originals=usb_originals_dir,
            usb_daemon_dir=usb_daemon_dir,
            out_dir=album_dir,
            cache=vision_cache_path,
            gateway=gateway_url,
            model=DEFAULT_VISION_MODEL,
            workers=28,
            target_count=None,
            limit=None,
            upgrade_originals=False,
        )
        run_pipeline(ns)
    else:
        upgraded_count = upgrade_originals_in_place(album_dir, usb_originals_dir, usb_daemon_dir)

    rebuilt_diptychs = verify_or_rebuild_diptychs(album_dir, force_rebuild=rebuild_diptychs)

    amazon_sync_result = None
    if sync_amazon:
        amazon_sync_result = sync_delta_to_amazon_photos(
            album_dir=album_dir,
            export_dir=amazon_export_dir,
            delta_dir=amazon_delta_dir,
            sync_state_path=amazon_sync_state,
        )
        amazon_summary = amazon_sync_result["export_summary"]
    else:
        amazon_summary = export_amazon_photos_bundle(
            album_dir=album_dir,
            export_dir=amazon_export_dir,
            delta_dir=amazon_delta_dir,
            sync_state_path=amazon_sync_state,
            mark_synced=mark_amazon_synced,
        )

    cloud_summary = None
    cloud_push_result = None
    if stage_cloud or push_cloud:
        ensure_private_access_key(key_file, rotate=False)
        cloud_summary = stage_cloud_bundle(
            album_dir=album_dir,
            stage_dir=cloud_stage_dir,
            sync_state_path=cloud_sync_state,
            bucket_name=cloud_bucket,
        )
        if push_cloud:
            cloud_push_result = push_staged_bundle_to_cloudflare(
                stage_dir=cloud_stage_dir,
                sync_state_path=cloud_sync_state,
                bucket_name=cloud_bucket,
                deploy_worker=deploy_worker,
            )

    tv_summary = None
    if sync_tv:
        resolved_tv_ip = resolve_tv_ip(tv_ip or None)
        tv_summary = export_tv_bundle(album_dir=album_dir, stage_dir=tv_stage_dir)
        deploy_to_tv(resolved_tv_ip, tv_stage_dir, install_ipk=False)
        tv_summary["tv_ip"] = resolved_tv_ip

    manifest = json.loads((album_dir / "manifest.json").read_text(encoding="utf-8"))
    stats = manifest.get("stats") or {}

    return {
        "upgraded_originals": upgraded_count,
        "rebuilt_diptychs": rebuilt_diptychs,
        "full_2048_photos": stats.get("full_2048_photos", 0),
        "thumb_640_photos": stats.get("thumb_640_photos", 0),
        "amazon_export": amazon_summary,
        "amazon_sync": amazon_sync_result,
        "cloud_stage": cloud_summary,
        "cloud_push": cloud_push_result,
        "tv_sync": tv_summary,
        "access_key_file": str(key_file) if (stage_cloud or push_cloud) else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--album-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--usb-originals", type=Path, default=DEFAULT_USB_ORIGINALS)
    parser.add_argument("--usb-daemon-dir", type=Path, default=DEFAULT_USB_DAEMON)
    parser.add_argument("--recurate", action="store_true", help="Re-run full catalog curation + diptychs + embeddings")
    parser.add_argument(
        "--refresh-catalog",
        action="store_true",
        help="Pull photos added since the last run from the live USBCameraBridge into curation_catalog.json (implies --recurate)",
    )
    parser.add_argument("--rebuild-diptychs", action="store_true", help="Force re-render all 1600x960 portrait diptychs")
    parser.add_argument("--mark-amazon-synced", action="store_true", help="Mark current Amazon Photos export hashes as uploaded")
    parser.add_argument("--sync-amazon", action="store_true", help="Automatically upload delta JPEGs to Amazon Photos 'Ambient Mix' via fmu")
    parser.add_argument("--open-amazon-photos", action="store_true", help="Explicit opt-in: open Amazon Photos album UI + delta folder")
    parser.add_argument("--stage-cloud", action="store_true", help="Optional: also stage Cloudflare R2 bundle locally")
    parser.add_argument("--push-cloud", action="store_true", help="Optional explicit opt-in: push changed objects to private Cloudflare R2")
    parser.add_argument("--deploy-worker", action="store_true", help="With --push-cloud, also run `wrangler deploy` for Worker/web code")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET_NAME)
    parser.add_argument("--sync-tv", action="store_true", help="Export and delta-sync 1080p slides directly onto LG webOS TV storage")
    parser.add_argument("--tv-ip", default="", help="LG webOS TV IPv4 address for --sync-tv")
    args = parser.parse_args()

    result = run_refresh(
        album_dir=args.album_dir,
        usb_originals_dir=args.usb_originals,
        usb_daemon_dir=args.usb_daemon_dir,
        recurate=args.recurate,
        refresh_iphone_catalog=args.refresh_catalog,
        rebuild_diptychs=args.rebuild_diptychs,
        mark_amazon_synced=args.mark_amazon_synced,
        sync_amazon=args.sync_amazon,
        cloud_bucket=args.bucket,
        stage_cloud=args.stage_cloud,
        push_cloud=args.push_cloud,
        deploy_worker=args.deploy_worker,
        sync_tv=args.sync_tv,
        tv_ip=args.tv_ip,
    )

    amz = result["amazon_export"]
    cld = result["cloud_stage"]
    print("\n=== Echo Show Standalone Refresh Summary ===", flush=True)
    print(
        f"1. Curated Masters:  {result['full_2048_photos']} at 2048px, {result['thumb_640_photos']} at 640px "
        f"(+{result['upgraded_originals']} upgraded this run, {result['rebuilt_diptychs']} diptychs re-rendered)",
        flush=True,
    )
    print(
        f"2. Amazon Photos:    {amz['total_slides']} landscape-ready JPEGs ({amz['total_mb']} MB, {amz['amazon_free_5gb_percent']}% of 5 GB free tier)\n"
        f"   - Full bundle:    {amz['export_dir']}\n"
        f"   - Upload delta:   {amz['pending_amazon_upload_count']} JPEGs in {amz['delta_dir']}",
        flush=True,
    )
    if cld:
        print(
            f"3. Optional Cloud:   {cld['total_objects']} R2 objects staged ({cld['total_mb']} MB)\n"
            f"   - Pending R2 put: {cld['pending_r2_upload_count']} objects (bucket: {cld['bucket_name']})",
            flush=True,
        )
        if result["cloud_push"]:
            print(f"   - Cloud push:     {json.dumps(result['cloud_push'])}", flush=True)
    if result.get("tv_sync"):
        tvs = result["tv_sync"]
        print(
            f"4. LG webOS TV:      {tvs['total_slides']} slides synced to {tvs['tv_ip']} ({tvs['total_mb']} MB, {tvs['ambient_slides']} in Ambient Mix)",
            flush=True,
        )

    if args.open_amazon_photos:
        reveal_dir = amz["delta_dir"] if amz["pending_amazon_upload_count"] > 0 else amz["export_dir"]
        subprocess.run(["open", reveal_dir], check=False)
        subprocess.run(["open", AMAZON_PHOTOS_ALBUMS_URL], check=False)


if __name__ == "__main__":
    main()

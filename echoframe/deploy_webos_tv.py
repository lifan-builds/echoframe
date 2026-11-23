#!/usr/bin/env python3
"""Export curated EchoFrame slides and deploy standalone idle screensaver, No Signal gallery,
local HTTP server (:18663), and launchable webOS app (`com.echoframe.webos`) directly to a
rooted LG webOS TV's internal storage (`/media/developer/echoframe`).

Usage:
    python3 -m echoframe.deploy_webos_tv --export-only
    python3 -m echoframe.deploy_webos_tv --tv-ip <TV_IP>
    python3 -m echoframe.deploy_webos_tv --tv-ip <TV_IP> --skip-ipk
    python3 -m echoframe.deploy_webos_tv --tv-ip <TV_IP> --uninstall
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import functools
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tarfile
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Any, Dict, List, Optional, Set, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from echoframe.extract_iphone_album import compose_gallery_matte  # noqa: E402
from echoframe.serve_album import is_slide_hidden, load_hidden_ids  # noqa: E402

DEFAULT_ALBUM_DIR = Path.cwd() / "data" / "album"
DEFAULT_STAGE_DIR = Path.cwd() / "data" / "export" / "webos_tv"
WEBOS_SRC_DIR = ROOT / "webos"
WEB_SRC_DIR = ROOT / "web"
TV_SYNC_CFG = Path.home() / ".config" / "make-tv-great-again" / "sync_config.json"

TV_BASE_DIR = "/media/developer/echoframe"
TV_APP_ID = "com.echoframe.webos"
TV_SERVICE_JS = "/media/developer/apps/usr/palm/services/com.hongguo.webos.service/service.js"
TV_INIT_SCRIPT = "/var/lib/webosbrew/init.d/echoframe"
HTTP_DEPLOY_PORT = 8095
TV_CANVAS_SIZE = (1920, 1080)
TV_JPEG_QUALITY = 86


def resolve_tv_ip(cli_ip: Optional[str]) -> str:
    if cli_ip:
        return cli_ip.strip()
    env_ip = os.environ.get("TV_IP", "").strip()
    if env_ip:
        return env_ip
    if TV_SYNC_CFG.is_file():
        try:
            cfg = json.loads(TV_SYNC_CFG.read_text(encoding="utf-8"))
            if cfg.get("tvIp"):
                return str(cfg["tvIp"]).strip()
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            print(f"Warning: could not read {TV_SYNC_CFG}: {exc}", file=sys.stderr)
    raise SystemExit("Pass --tv-ip <TV_IP> or set TV_IP.")


def get_local_ip(target_ip: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 80))
        return s.getsockname()[0]
    finally:
        s.close()


def send_telnet_cmd(tv_ip: str, cmd: str, timeout_sec: float = 90.0) -> str:
    marker = "EF_DEPLOY__DONE"
    marker_bytes = marker.encode("ascii")
    with socket.create_connection((tv_ip, 23), timeout=10) as s:
        s.settimeout(0.5)
        time.sleep(0.3)
        try:
            s.recv(4096)
        except socket.timeout:
            pass
        s.settimeout(timeout_sec)
        payload = f"{cmd}\necho EF_DEPLOY_\"\"_DONE\n".encode("utf-8")
        s.sendall(payload)
        res = b""
        t0 = time.time()
        while time.time() - t0 < timeout_sec:
            remaining = max(0.1, timeout_sec - (time.time() - t0))
            s.settimeout(remaining)
            try:
                chunk = s.recv(8192)
            except socket.timeout as exc:
                raise TimeoutError(f"Telnet command timed out after {timeout_sec:.0f}s: {cmd[:80]}") from exc
            if not chunk:
                raise ConnectionError(f"Telnet connection closed before completion marker: {cmd[:80]}")
            res += chunk
            if marker_bytes in res:
                return res.decode("utf-8", errors="replace")
        raise TimeoutError(f"Telnet command timed out after {timeout_sec:.0f}s: {cmd[:80]}")


def render_tv_1080p_jpeg(
    src_path: Path,
    accent_rgb: List[int],
    kind: str = "single",
    canvas_size: Tuple[int, int] = TV_CANVAS_SIZE,
    quality: int = TV_JPEG_QUALITY,
) -> bytes:
    """Render a crisp 1920x1080 (16:9) baseline JPEG suitable for both QML and LG photorenderer."""
    cw, ch = canvas_size
    with Image.open(src_path) as raw_im:
        im = ImageOps.exif_transpose(raw_im).convert("RGB")
        aspect = im.width / float(max(1, im.height))

        if kind != "diptych" and 1.70 <= aspect <= 1.86:
            final_rgb = ImageOps.fit(im, canvas_size, method=Image.Resampling.LANCZOS)
        else:
            if kind == "diptych":
                max_w, max_h = cw, ch
            elif aspect >= 1.15:
                max_w, max_h = cw, ch
            else:
                max_w, max_h = cw - 200, ch - 96

            scale = min(max_w / float(max(1, im.width)), max_h / float(max(1, im.height)))
            card_w = max(1, int(round(im.width * scale)))
            card_h = max(1, int(round(im.height * scale)))
            tile = im.resize((card_w, card_h), Image.Resampling.LANCZOS)
            x_left = (cw - card_w) // 2
            y_top = (ch - card_h) // 2
            final_rgb = compose_gallery_matte(
                canvas_size,
                (accent_rgb or [24, 32, 44])[:3],
                [(tile, x_left, y_top)],
                blend=0.16,
                rx_scale=0.56,
                ry_scale=0.62,
            )

    buf = io.BytesIO()
    # Baseline (progressive=False) guarantees hardware JPEG decode compatibility in LG photorenderer.
    final_rgb.save(buf, format="JPEG", quality=quality, progressive=False, optimize=True)
    return buf.getvalue()


def build_echoframe_icons(app_dir: Path) -> None:
    """Render 130x130 and 260x260 webOS launcher icons for EchoFrame."""
    size = 520
    base = Image.new("RGBA", (size, size), (11, 14, 19, 255))
    px = base.load()
    r1, g1, b1 = (28, 36, 52)
    r2, g2, b2 = (12, 16, 24)
    for y in range(size):
        ty = y / float(size - 1)
        for x in range(size):
            tx = x / float(size - 1)
            t = 0.72 * ty + 0.28 * tx
            dx = (x - size * 0.5) / (size * 0.60)
            dy = (y - size * 0.20) / (size * 0.52)
            glow = max(0.0, 1.0 - (dx * dx + dy * dy)) * 0.16
            r = int(min(255, (r1 + (r2 - r1) * t) * (1.0 - glow) + 231 * glow))
            g = int(min(255, (g1 + (g2 - g1) * t) * (1.0 - glow) + 188 * glow))
            b = int(min(255, (b1 + (b2 - b1) * t) * (1.0 - glow) + 121 * glow))
            px[x, y] = (r, g, b, 255)

    draw = ImageDraw.Draw(base, "RGBA")
    # Gallery frame motif
    draw.rounded_rectangle([86, 82, 434, 314], radius=24, fill=(18, 23, 33, 220), outline=(231, 188, 121, 225), width=8)
    draw.rounded_rectangle([110, 106, 410, 290], radius=12, fill=(32, 42, 60, 255))
    # Stylized horizon + warm sun inside frame
    draw.ellipse([306, 134, 366, 194], fill=(231, 188, 121, 245))
    draw.polygon([(110, 290), (206, 192), (288, 252), (344, 214), (410, 290)], fill=(15, 20, 30, 235))

    serif_path = WEB_SRC_DIR / "fonts" / "InstrumentSerif-Regular.ttf"
    if serif_path.is_file():
        font = ImageFont.truetype(str(serif_path), 104)
    else:
        font = ImageFont.load_default()
    label = "EchoFrame"
    bbox = draw.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    tx = (size - tw) / 2.0 - bbox[0]
    ty = 342 - bbox[1]

    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((tx, ty + 6), label, font=font, fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=4))
    base = Image.alpha_composite(base, shadow)
    ImageDraw.Draw(base).text((tx, ty), label, font=font, fill=(245, 242, 235, 255))

    for out_name, dim in (("icon.png", 130), ("largeIcon.png", 260)):
        resized = base.resize((dim, dim), Image.Resampling.LANCZOS)
        resized.save(app_dir / out_name, format="PNG", optimize=True)


def _ar_member(name: str, content: bytes) -> bytes:
    ts = str(int(time.time()))
    hdr = f"{name:<16}{ts:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}`\n".encode("ascii")
    assert len(hdr) == 60
    pad = b"\n" if (len(content) % 2 == 1) else b""
    return hdr + content + pad


def _make_targz_from_dir(root_dir: Path, top_name: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.USTAR_FORMAT) as tf:
        for dirpath, dirnames, filenames in os.walk(root_dir / top_name):
            dirnames.sort()
            filenames.sort()
            rel_dir = os.path.relpath(dirpath, root_dir)
            ti = tarfile.TarInfo(rel_dir + "/")
            ti.type = tarfile.DIRTYPE
            ti.mode = 0o755
            ti.mtime = int(time.time())
            tf.addfile(ti)
            for fn in filenames:
                full = Path(dirpath) / fn
                rel = os.path.relpath(full, root_dir)
                data = full.read_bytes()
                fi = tarfile.TarInfo(rel)
                fi.size = len(data)
                fi.mode = 0o644
                fi.mtime = int(time.time())
                tf.addfile(fi, io.BytesIO(data))
    return buf.getvalue()


def build_echoframe_ipk(stage_dir: Path) -> Path:
    pkg_src = WEBOS_SRC_DIR / "pkg"
    pkg_tmp = stage_dir / "_pkg_build"
    if pkg_tmp.exists():
        shutil.rmtree(pkg_tmp)
    shutil.copytree(pkg_src, pkg_tmp)

    app_dir = pkg_tmp / "usr" / "palm" / "applications" / TV_APP_ID
    build_echoframe_icons(app_dir)

    data_targz = _make_targz_from_dir(pkg_tmp, "usr")
    control_txt = (
        f"Package: {TV_APP_ID}\n"
        "Version: 1.0.0\n"
        "Section: misc\n"
        "Priority: optional\n"
        "Architecture: all\n"
        "Installed-Size: 64000\n"
        "Maintainer: EchoFrame <echoframe@local>\n"
        "Description: EchoFrame Ambient Photo Album for LG webOS.\n"
        "webOS-Package-Format-Version: 2\n"
        "webOS-Packager-Version: x.y.x\n"
    ).encode("utf-8")

    cbuf = io.BytesIO()
    with tarfile.open(fileobj=cbuf, mode="w:gz", format=tarfile.USTAR_FORMAT) as tf:
        ci = tarfile.TarInfo("control")
        ci.size = len(control_txt)
        ci.mode = 0o644
        ci.mtime = int(time.time())
        tf.addfile(ci, io.BytesIO(control_txt))

    ipk_bytes = (
        b"!<arch>\n"
        + _ar_member("debian-binary", b"2.0\n")
        + _ar_member("control.tar.gz", cbuf.getvalue())
        + _ar_member("data.tar.gz", data_targz)
    )
    out_ipk = stage_dir / f"{TV_APP_ID}.ipk"
    out_ipk.write_bytes(ipk_bytes)
    shutil.rmtree(pkg_tmp, ignore_errors=True)
    return out_ipk


def generate_screensaver_photo_list_js(ambient_filenames: List[str]) -> str:
    """Generate drop-in replacement for com.webos.app.inputcommon ScreensaverPhotoList.js."""
    files_json = json.dumps(ambient_filenames, ensure_ascii=False, indent=4)
    return (
        f'var parentPath = "{TV_BASE_DIR}/slides/";\n'
        f"var echoFrameFiles = {files_json};\n"
        'var panelType = {"fhd": 0, "uhd": 1, "oled": 2, "bno": 3, "suhd": 4};\n'
        "function getImgList(panel) {\n"
        "    var out = [];\n"
        "    for (var i = 0; i < echoFrameFiles.length; ++i) {\n"
        "        out.push(parentPath + echoFrameFiles[i]);\n"
        "    }\n"
        "    return out;\n"
        "}\n"
    )


def generate_init_script() -> str:
    return f"""#!/bin/sh
BASE="{TV_BASE_DIR}"

bind_file() {{
    src="$1"
    dst="$2"
    if [ -f "$src" ] && [ -f "$dst" ]; then
        umount "$dst" 2>/dev/null || true
        mount --bind "$src" "$dst" 2>/dev/null || true
    fi
}}

# 1. Idle Screensaver (com.webos.app.screensaver)
bind_file "$BASE/qml/MainView.qml" "/usr/palm/applications/com.webos.app.screensaver/qml/Containers/MainView.qml"
bind_file "$BASE/qml/ScreenSaver.qml" "/usr/palm/applications/com.webos.app.screensaver/qml/Containers/ScreenSaver.qml"

# 2. No Signal HDMI / Live TV photo list & full-screen renderer (com.webos.app.inputcommon)
bind_file "$BASE/qml/ScreensaverPhotoList.js" "/usr/palm/applications/com.webos.app.inputcommon/qml/Model/ScreensaverPhotoList.js"
bind_file "$BASE/qml/PhotoPlayer.qml" "/usr/palm/applications/com.webos.app.inputcommon/qml/TvComponent/ScreenSaver/PhotoPlayer.qml"

# Clear stale QML checkpoints so updated QML/JS loads cleanly
rm -rf /tmp/checkpoint/com.webos.app.screensaver /mnt/lg/cmn_data/checkpoint/com.webos.app.screensaver \\
       /tmp/checkpoint/com.webos.app.inputcommon /mnt/lg/cmn_data/checkpoint/com.webos.app.inputcommon 2>/dev/null || true

# Reload screensaver daemon if currently resident
killall -9 com.webos.app.screensaver 2>/dev/null || true

# Ensure local HTTP server is active (reuses existing shared TV Node service if present, else standalone)
if [ -f "{TV_SERVICE_JS}" ]; then
    if ! grep -q "echoframe/tv_server.js" "{TV_SERVICE_JS}" 2>/dev/null; then
        cat << 'EFHOOK' >> "{TV_SERVICE_JS}"

try {{
  if (typeof process !== 'undefined' && !IS_CF_WORKER && fs.existsSync('{TV_BASE_DIR}/tv_server.js')) {{
    require('{TV_BASE_DIR}/tv_server.js');
  }}
}} catch (efErr) {{
  console.error('[echoframe] Server init error:', efErr && efErr.message);
}}
EFHOOK
    fi
else
    pkill -f "echoframe/tv_server.js" 2>/dev/null || true
    nohup /usr/bin/node "{TV_BASE_DIR}/tv_server.js" >/tmp/echoframe.log 2>&1 &
fi
"""


def export_tv_bundle(
    album_dir: Path,
    stage_dir: Path,
    ambient_only: bool = False,
    quality: int = TV_JPEG_QUALITY,
) -> Dict[str, Any]:
    manifest_path = album_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Album manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hidden_ids: Set[str] = load_hidden_ids(album_dir)
    raw_slides: List[Dict[str, Any]] = manifest.get("slides") or []

    slides_out_dir = stage_dir / "slides"
    qml_out_dir = stage_dir / "qml"
    web_out_dir = stage_dir / "web"
    fonts_out_dir = stage_dir / "fonts"
    for d in (slides_out_dir, qml_out_dir, web_out_dir, fonts_out_dir):
        d.mkdir(parents=True, exist_ok=True)

    sync_state_path = stage_dir / "tv_sync_state.json"
    prev_state: Dict[str, Any] = {}
    if sync_state_path.is_file():
        try:
            prev_state = json.loads(sync_state_path.read_text(encoding="utf-8"))
        except Exception:
            prev_state = {}
    prev_hashes: Dict[str, str] = prev_state.get("source_sig", {}) if isinstance(prev_state, dict) else {}

    selected_slides: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    for s in raw_slides:
        sid = str(s.get("id") or "")
        if not sid or sid in seen_ids or is_slide_hidden(s, hidden_ids):
            continue
        if ambient_only and s.get("in_default_frame") is False:
            continue
        rel = str(s.get("rel_path") or s.get("src", "").lstrip("/"))
        src_file = album_dir / rel
        if not src_file.is_file():
            continue
        seen_ids.add(sid)
        selected_slides.append(s)

    new_hashes: Dict[str, str] = {}
    tasks: List[Tuple[Dict[str, Any], Path, Path, str]] = []
    expected_filenames: Set[str] = set()

    for s in selected_slides:
        sid = str(s["id"])
        filename = f"{sid}.jpg"
        expected_filenames.add(filename)
        rel = str(s.get("rel_path") or s.get("src", "").lstrip("/"))
        src_file = album_dir / rel
        dst_file = slides_out_dir / filename
        st = src_file.stat()
        sig = f"{st.st_size}:{int(st.st_mtime)}:{quality}:{s.get('kind', 'single')}:{s.get('accent_rgb')}"
        new_hashes[filename] = sig
        if dst_file.is_file() and prev_hashes.get(filename) == sig:
            continue
        tasks.append((s, src_file, dst_file, sig))

    # Remove stale slide files
    for existing in slides_out_dir.glob("*.jpg"):
        if existing.name not in expected_filenames:
            existing.unlink()

    def _worker(item: Tuple[Dict[str, Any], Path, Path, str]) -> int:
        s, src_file, dst_file, _ = item
        jpg = render_tv_1080p_jpeg(
            src_file,
            s.get("accent_rgb") or [24, 32, 44],
            kind=str(s.get("kind") or "single"),
            quality=quality,
        )
        dst_file.write_bytes(jpg)
        return len(jpg)

    rendered = 0
    if tasks:
        print(f"Rendering {len(tasks)} TV 1080p baseline slides (cached: {len(selected_slides) - len(tasks)})...", flush=True)
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as ex:
            for _ in ex.map(_worker, tasks):
                rendered += 1

    tv_slides: List[Dict[str, Any]] = []
    ambient_filenames: List[str] = []
    fallback_qml_slides: List[Dict[str, Any]] = []
    total_bytes = 0

    for s in selected_slides:
        sid = str(s["id"])
        filename = f"{sid}.jpg"
        dst_file = slides_out_dir / filename
        total_bytes += dst_file.stat().st_size

        tv_slide = {
            "id": sid,
            "filename": filename,
            "src": f"/slides/{filename}",
            "kind": s.get("kind", "single"),
            "member_ids": s.get("member_ids", []),
            "photo_id": s.get("photo_id", ""),
            "orientation": "landscape",
            "aspect_ratio": 1.7778,
            "width": TV_CANVAS_SIZE[0],
            "height": TV_CANVAS_SIZE[1],
            "creation_date": s.get("creation_date", ""),
            "year": s.get("year", ""),
            "month_day": s.get("month_day", ""),
            "date_label": s.get("date_label", ""),
            "age_badge": s.get("age_badge") or "",
            "frame_score": s.get("frame_score", 8.0),
            "channels": s.get("channels", []),
            "color_palette": s.get("color_palette", ""),
            "visual_theme": s.get("visual_theme", ""),
            "display_caption": s.get("display_caption") or s.get("label") or "",
            "search_detail": s.get("search_detail", ""),
            "tags": s.get("tags", []),
            "accent_rgb": s.get("accent_rgb") or [24, 32, 44],
            "in_default_frame": s.get("in_default_frame", True),
        }
        tv_slides.append(tv_slide)
        if tv_slide["in_default_frame"] is not False:
            ambient_filenames.append(filename)
            fallback_qml_slides.append({
                "id": sid,
                "filename": filename,
                "display_caption": tv_slide["display_caption"],
                "date_label": tv_slide["date_label"],
                "age_badge": tv_slide["age_badge"],
                "accent_rgb": tv_slide["accent_rgb"],
            })

    tv_manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stats": {
            "total_slides": len(tv_slides),
            "ambient_mix_slides": len(ambient_filenames),
            "total_bytes": total_bytes,
        },
        "channels": manifest.get("channels", []),
        "slides": tv_slides,
    }
    (stage_dir / "manifest.json").write_text(json.dumps(tv_manifest, ensure_ascii=False), encoding="utf-8")

    # Stage QML files with embedded offline fallback
    shutil.copyfile(WEBOS_SRC_DIR / "qml" / "MainView.qml", qml_out_dir / "MainView.qml")
    shutil.copyfile(WEBOS_SRC_DIR / "qml" / "PhotoPlayer.qml", qml_out_dir / "PhotoPlayer.qml")
    ss_tpl = (WEBOS_SRC_DIR / "qml" / "ScreenSaver.qml").read_text(encoding="utf-8")
    fallback_json = json.dumps(fallback_qml_slides, ensure_ascii=False, separators=(",", ":"))
    ss_out = ss_tpl.replace("/*__FALLBACK_SLIDES__*/[]", fallback_json)
    (qml_out_dir / "ScreenSaver.qml").write_text(ss_out, encoding="utf-8")
    (qml_out_dir / "ScreensaverPhotoList.js").write_text(
        generate_screensaver_photo_list_js(ambient_filenames),
        encoding="utf-8",
    )

    # Stage tv_server.js, init script, web UI, fonts, and IPK
    shutil.copyfile(WEBOS_SRC_DIR / "tv_server.js", stage_dir / "tv_server.js")
    (stage_dir / "init_echoframe.sh").write_text(generate_init_script(), encoding="utf-8")

    for fn in ("index.html", "gallery.html", "remote.html", "album.css", "album.js", "app.css", "gallery.js", "remote.js", "sw.js", "silence.wav"):
        src_w = WEB_SRC_DIR / fn
        if src_w.is_file():
            shutil.copyfile(src_w, web_out_dir / fn)

    src_fonts = WEB_SRC_DIR / "fonts"
    if src_fonts.is_dir():
        for font_file in src_fonts.glob("*.ttf"):
            shutil.copyfile(font_file, fonts_out_dir / font_file.name)

    ipk_path = build_echoframe_ipk(stage_dir)

    sync_state_path.write_text(
        json.dumps({"source_sig": new_hashes, "updated_at": time.time()}, indent=2),
        encoding="utf-8",
    )

    return {
        "total_slides": len(tv_slides),
        "ambient_slides": len(ambient_filenames),
        "rendered_now": rendered,
        "total_mb": round(total_bytes / (1024 * 1024), 1),
        "ipk_path": str(ipk_path),
    }


def query_remote_slides(tv_ip: str) -> Dict[str, int]:
    raw = send_telnet_cmd(
        tv_ip,
        f"if [ -d {TV_BASE_DIR}/slides ]; then "
        f"ls -l {TV_BASE_DIR}/slides | awk '/\\.jpg$/ {{print $9 \" \" $5}}'; fi",
        timeout_sec=15.0,
    )
    existing: Dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[0].endswith(".jpg") and parts[1].isdigit():
            existing[parts[0]] = int(parts[1])
    return existing


def deploy_to_tv(tv_ip: str, stage_dir: Path, install_ipk: bool = True, restart_inputcommon: bool = True) -> None:
    mac_ip = get_local_ip(tv_ip)
    slides_dir = stage_dir / "slides"
    local_slides = {p.name: p.stat().st_size for p in sorted(slides_dir.glob("*.jpg"))}
    remote_slides = query_remote_slides(tv_ip)

    to_upload = [fn for fn, sz in local_slides.items() if remote_slides.get(fn) != sz]
    to_delete = [fn for fn in remote_slides if fn not in local_slides]
    print(
        f"TV {tv_ip}: {len(local_slides)} local slides, {len(remote_slides)} on TV "
        f"-> uploading {len(to_upload)} delta slides, deleting {len(to_delete)} stale",
        flush=True,
    )

    http_dir = stage_dir / "_http_tmp"
    if http_dir.exists():
        shutil.rmtree(http_dir)
    http_dir.mkdir(parents=True)

    # 1. Core bundle (manifest, qml, web, fonts, tv_server.js, init_echoframe.sh)
    core_tar = http_dir / "echoframe_core.tar.gz"
    with tarfile.open(core_tar, "w:gz") as tf:
        for rel in ("manifest.json", "tv_server.js", "init_echoframe.sh", "qml", "web", "fonts"):
            p = stage_dir / rel
            if p.exists():
                tf.add(p, arcname=rel)

    # 2. Delta slides tarball (uncompressed .tar — JPEGs are already compressed, so tar is much faster on TV CPU!)
    slides_tar = http_dir / "echoframe_slides_delta.tar"
    if to_upload:
        with tarfile.open(slides_tar, "w") as tf:
            for fn in to_upload:
                tf.add(slides_dir / fn, arcname=f"slides/{fn}")

    ipk_file = stage_dir / f"{TV_APP_ID}.ipk"
    if install_ipk and ipk_file.is_file():
        shutil.copyfile(ipk_file, http_dir / ipk_file.name)

    steps = [
        f"mkdir -p {TV_BASE_DIR}/slides {TV_BASE_DIR}/qml {TV_BASE_DIR}/web {TV_BASE_DIR}/fonts /var/lib/webosbrew/init.d",
        f"wget -q -O /tmp/ef_core.tar.gz http://{mac_ip}:{HTTP_DEPLOY_PORT}/echoframe_core.tar.gz",
        f"tar -xzf /tmp/ef_core.tar.gz -C {TV_BASE_DIR} && rm -f /tmp/ef_core.tar.gz",
    ]
    if to_upload:
        steps.extend([
            f"wget -q -O /tmp/ef_slides.tar http://{mac_ip}:{HTTP_DEPLOY_PORT}/echoframe_slides_delta.tar",
            f"tar -xf /tmp/ef_slides.tar -C {TV_BASE_DIR} && rm -f /tmp/ef_slides.tar",
        ])
    if to_delete:
        rm_args = " ".join(f"{TV_BASE_DIR}/slides/{fn}" for fn in to_delete)
        steps.append(f"rm -f {rm_args}")

    steps.extend([
        f"cp {TV_BASE_DIR}/init_echoframe.sh {TV_INIT_SCRIPT} && chmod +x {TV_INIT_SCRIPT}",
        f"{TV_INIT_SCRIPT}",
    ])

    if install_ipk and ipk_file.is_file():
        steps.extend([
            f"wget -q -O /tmp/{TV_APP_ID}.ipk http://{mac_ip}:{HTTP_DEPLOY_PORT}/{TV_APP_ID}.ipk",
            f"(luna-send -i 'luna://com.webos.appInstallService/dev/install' "
            f"'{{\"id\":\"{TV_APP_ID}\",\"ipkUrl\":\"/tmp/{TV_APP_ID}.ipk\",\"subscribe\":true}}' >/dev/null 2>&1 &)",
            "sleep 6; killall luna-send 2>/dev/null || true",
            f"rm -f /tmp/{TV_APP_ID}.ipk",
        ])

    # Restart shared TV Node service if installed so port 18663 starts cleanly
    steps.extend([
        "if [ -x /var/lib/webosbrew/init.d/hongguo ]; then "
        "for p in $(ps aux | grep '[c]om.hongguo.webos.service/service.js' | awk '{print $2}'); do kill -9 $p 2>/dev/null; done; "
        "/var/lib/webosbrew/init.d/hongguo; fi",
        "sleep 2",
    ])

    if restart_inputcommon:
        # Restart inputcommon so No Signal immediately picks up the bind-mounted ScreensaverPhotoList.js
        steps.append("killall -9 com.webos.app.inputcommon 2>/dev/null || true")

    steps.extend([
        f"df -h /media | tail -n 1",
        f"ls {TV_BASE_DIR}/slides | wc -l",
        "mount | grep -E 'screensaver|inputcommon' || true",
    ])

    run_sh = http_dir / "run_ef_deploy.sh"
    run_sh.write_text("#!/bin/sh\nset -e\n" + "\n".join(steps) + "\n", encoding="utf-8")

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(http_dir))
    srv = HTTPServer(("0.0.0.0", HTTP_DEPLOY_PORT), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        out = send_telnet_cmd(
            tv_ip,
            f"wget -q -O /tmp/run_ef_deploy.sh http://{mac_ip}:{HTTP_DEPLOY_PORT}/run_ef_deploy.sh "
            f"&& sh /tmp/run_ef_deploy.sh; rm -f /tmp/run_ef_deploy.sh",
            timeout_sec=180.0,
        )
        print(out)
    finally:
        srv.shutdown()
        shutil.rmtree(http_dir, ignore_errors=True)


def uninstall_from_tv(tv_ip: str) -> None:
    cmd = "\n".join([
        "umount /usr/palm/applications/com.webos.app.screensaver/qml/Containers/MainView.qml 2>/dev/null || true",
        "umount /usr/palm/applications/com.webos.app.screensaver/qml/Containers/ScreenSaver.qml 2>/dev/null || true",
        "umount /usr/palm/applications/com.webos.app.inputcommon/qml/Model/ScreensaverPhotoList.js 2>/dev/null || true",
        "umount /usr/palm/applications/com.webos.app.inputcommon/qml/TvComponent/ScreenSaver/PhotoPlayer.qml 2>/dev/null || true",
        f"rm -f {TV_INIT_SCRIPT}",
        f"(luna-send -n 1 'luna://com.webos.appInstallService/dev/remove' '{{\"id\":\"{TV_APP_ID}\",\"subscribe\":false}}' >/dev/null 2>&1 &)",
        "killall -9 com.webos.app.screensaver 2>/dev/null || true",
        "echo Uninstalled EchoFrame overlays from TV.",
    ])
    print(send_telnet_cmd(tv_ip, cmd, timeout_sec=30.0))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export and deploy EchoFrame directly onto LG webOS TV storage")
    parser.add_argument("--album-dir", type=Path, default=DEFAULT_ALBUM_DIR)
    parser.add_argument("--stage-dir", type=Path, default=DEFAULT_STAGE_DIR)
    parser.add_argument("--tv-ip", help="LG webOS TV IPv4 address on LAN")
    parser.add_argument("--ambient-only", action="store_true", help="Export only the default Ambient Mix channel")
    parser.add_argument("--quality", type=int, default=TV_JPEG_QUALITY, help="Baseline JPEG quality (default 86)")
    parser.add_argument("--export-only", action="store_true", help="Only build local TV bundle without deploying")
    parser.add_argument("--skip-ipk", action="store_true", help="Skip reinstalling com.echoframe.webos IPK")
    parser.add_argument("--uninstall", action="store_true", help="Unmount EchoFrame overlays and remove TV boot hook")
    args = parser.parse_args()

    if args.uninstall:
        uninstall_from_tv(resolve_tv_ip(args.tv_ip))
        return

    summary = export_tv_bundle(
        album_dir=args.album_dir,
        stage_dir=args.stage_dir,
        ambient_only=args.ambient_only,
        quality=args.quality,
    )
    print(json.dumps(summary, indent=2), flush=True)

    if args.export_only:
        return

    tv_ip = resolve_tv_ip(args.tv_ip)
    deploy_to_tv(tv_ip, args.stage_dir, install_ipk=not args.skip_ipk)


if __name__ == "__main__":
    main()

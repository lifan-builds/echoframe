import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
from PIL import Image

from echoframe.export_amazon_photos import export_amazon_photos_bundle
from echoframe.refresh_standalone import run_refresh
from echoframe.stage_cloud_bundle import ensure_private_access_key, stage_cloud_bundle

ROOT = Path(__file__).resolve().parents[1]


def make_test_jpeg(path: Path, size=(640, 480), color=(90, 120, 150)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, color)
    im.save(path, format="JPEG", quality=90)


class StandaloneExportAndCloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.album = self.base / "album"
        (self.album / "photos").mkdir(parents=True)
        (self.album / "diptychs").mkdir(parents=True)

        # p_001: landscape single
        make_test_jpeg(self.album / "photos" / "p_001.jpg", size=(640, 420), color=(60, 100, 140))
        # p_002: unpaired portrait single (should be matted to 1600x960)
        make_test_jpeg(self.album / "photos" / "p_002.jpg", size=(420, 640), color=(150, 110, 80))
        # p_003 & p_004: paired portraits inside diptych d_001
        make_test_jpeg(self.album / "photos" / "p_003.jpg", size=(420, 640), color=(80, 130, 90))
        make_test_jpeg(self.album / "photos" / "p_004.jpg", size=(420, 640), color=(85, 125, 95))
        make_test_jpeg(self.album / "diptychs" / "d_001.jpg", size=(1600, 960), color=(20, 28, 36))

        manifest = {
            "version": 2,
            "channels": [
                {"id": "smart_frame", "name": "Ambient Mix", "description": "Default"},
                {"id": "family", "name": "Family & Friends", "description": "Family"},
                {"id": "travel_outdoors", "name": "Mountains & Travel", "description": "Travel"},
            ],
            "stats": {
                "curated_photos": 4,
                "diptych_slides": 1,
                "full_2048_photos": 0,
                "thumb_640_photos": 4,
            },
            "photos": [
                {
                    "id": "p_001",
                    "local_id": "IMG_0001.HEIC",
                    "src": "/photos/p_001.jpg",
                    "rel_path": "photos/p_001.jpg",
                    "width": 640,
                    "height": 420,
                    "aspect_ratio": 1.5238,
                    "resolution_tier": "thumb_640",
                    "focal_xy": [0.5, 0.45],
                    "accent_rgb": [60, 100, 140],
                },
                {
                    "id": "p_002",
                    "local_id": "IMG_0002.HEIC",
                    "src": "/photos/p_002.jpg",
                    "rel_path": "photos/p_002.jpg",
                    "width": 420,
                    "height": 640,
                    "aspect_ratio": 0.6563,
                    "resolution_tier": "thumb_640",
                    "focal_xy": [0.5, 0.4],
                    "accent_rgb": [150, 110, 80],
                },
                {
                    "id": "p_003",
                    "local_id": "IMG_0003.HEIC",
                    "src": "/photos/p_003.jpg",
                    "rel_path": "photos/p_003.jpg",
                    "width": 420,
                    "height": 640,
                    "aspect_ratio": 0.6563,
                    "resolution_tier": "thumb_640",
                    "focal_xy": [0.5, 0.4],
                    "accent_rgb": [80, 130, 90],
                },
                {
                    "id": "p_004",
                    "local_id": "IMG_0004.HEIC",
                    "src": "/photos/p_004.jpg",
                    "rel_path": "photos/p_004.jpg",
                    "width": 420,
                    "height": 640,
                    "aspect_ratio": 0.6563,
                    "resolution_tier": "thumb_640",
                    "focal_xy": [0.5, 0.4],
                    "accent_rgb": [85, 125, 95],
                },
            ],
            "slides": [
                {
                    "id": "d_001",
                    "kind": "diptych",
                    "member_ids": ["p_003", "p_004"],
                    "src": "/diptychs/d_001.jpg",
                    "rel_path": "diptychs/d_001.jpg",
                    "aspect_ratio": 1.6667,
                    "orientation": "landscape",
                    "resolution_tier": "thumb_640",
                    "creation_date": "2025-02-14T18:30:00.000Z",
                    "month_day": "02-14",
                    "date_label": "Feb 2025",
                    "display_caption": "Alpine ridge powder run",
                    "visual_theme": "snow_mountain",
                    "channels": ["travel_outdoors"],
                    "tags": ["ski", "powder", "mountain"],
                    "accent_rgb": [40, 60, 80],
                    "in_default_frame": True,
                    "frame_score": 9.3,
                },
                {
                    "id": "p_001",
                    "photo_id": "p_001",
                    "kind": "single",
                    "src": "/photos/p_001.jpg",
                    "rel_path": "photos/p_001.jpg",
                    "aspect_ratio": 1.5238,
                    "orientation": "landscape",
                    "resolution_tier": "thumb_640",
                    "creation_date": "2024-08-09T01:15:20.000Z",
                    "month_day": "08-09",
                    "date_label": "Aug 2024",
                    "display_caption": "Golden sunset over ocean cliffs",
                    "visual_theme": "sunset_coast",
                    "channels": ["travel_outdoors"],
                    "tags": ["ocean", "sunset", "beach"],
                    "accent_rgb": [60, 100, 140],
                    "in_default_frame": True,
                    "frame_score": 8.9,
                },
                {
                    "id": "p_002",
                    "photo_id": "p_002",
                    "kind": "single",
                    "src": "/photos/p_002.jpg",
                    "rel_path": "photos/p_002.jpg",
                    "aspect_ratio": 0.6563,
                    "orientation": "portrait",
                    "resolution_tier": "thumb_640",
                    "creation_date": "2026-06-10T14:05:00.000Z",
                    "month_day": "06-10",
                    "date_label": "Jun 2026",
                    "age_badge": "10 wks",
                    "is_subject": True,
                    "display_caption": "Quiet morning yawn",
                    "visual_theme": "baby_cuddle",
                    "channels": ["family"],
                    "tags": ["baby", "cuddle"],
                    "accent_rgb": [150, 110, 80],
                    "in_default_frame": True,
                    "frame_score": 9.0,
                },
                {
                    # Duplicate solo coverage of p_003 even if flagged in_default_frame=True:
                    # exporter must deduplicate because p_003 is already inside d_001!
                    "id": "p_003",
                    "photo_id": "p_003",
                    "kind": "single",
                    "src": "/photos/p_003.jpg",
                    "rel_path": "photos/p_003.jpg",
                    "aspect_ratio": 0.6563,
                    "orientation": "portrait",
                    "resolution_tier": "thumb_640",
                    "creation_date": "2025-02-14T18:00:00.000Z",
                    "display_caption": "Trailhead pine snow",
                    "channels": ["travel_outdoors"],
                    "accent_rgb": [80, 130, 90],
                    "in_default_frame": True,
                },
            ],
        }
        (self.album / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        vecs = np.array(
            [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.7, 0.7, 0.0, 0.0],
            ],
            dtype=np.float32,
        )
        np.savez_compressed(
            self.album / "album_embeddings.npz",
            ids=np.array(["d_001", "p_001", "p_002", "p_003"]),
            vectors=vecs,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_amazon_export_dedup_matte_exif_and_delta_sync(self):
        export_dir = self.base / "export_amz"
        delta_dir = self.base / "delta_amz"
        state_path = self.base / "amz_state.json"

        export_dir.mkdir(parents=True)
        (export_dir / ".DS_Store").write_bytes(b"junk")

        summary1 = export_amazon_photos_bundle(
            album_dir=self.album,
            export_dir=export_dir,
            delta_dir=delta_dir,
            sync_state_path=state_path,
        )
        # p_003 was deduplicated because it is already part of d_001 -> 3 unique slides!
        self.assertEqual(summary1["total_slides"], 3)
        self.assertEqual(summary1["diptych_16x10_slides"], 1)
        self.assertEqual(summary1["landscape_singles"], 1)
        self.assertEqual(summary1["matted_16x10_singles"], 1)
        self.assertEqual(summary1["pending_amazon_upload_count"], 3)

        # Pure .jpg directory (stray .DS_Store removed)
        exported_names = sorted(p.name for p in export_dir.iterdir())
        self.assertEqual(exported_names, ["d_001.jpg", "p_001.jpg", "p_002.jpg"])

        # Portrait p_002 was matted onto 1600x960 (16:10) and preserves EXIF DateTimeOriginal
        with Image.open(export_dir / "p_002.jpg") as im_matted:
            self.assertEqual(im_matted.size, (1600, 960))
            exif = im_matted.getexif()
            self.assertEqual(exif.get_ifd(0x8769).get(0x9003), "2026:06:10 14:05:00")

        # Landscape p_001 kept its native 640x420 pixels losslessly AND got EXIF DateTimeOriginal
        with Image.open(export_dir / "p_001.jpg") as im_land:
            self.assertEqual(im_land.size, (640, 420))
            exif_l = im_land.getexif()
            self.assertEqual(exif_l.get_ifd(0x8769).get(0x9003), "2024:08:09 01:15:20")

        # Mark synced -> delta_dir becomes empty on immediate re-run
        summary2 = export_amazon_photos_bundle(
            album_dir=self.album,
            export_dir=export_dir,
            delta_dir=delta_dir,
            sync_state_path=state_path,
            mark_synced=True,
        )
        self.assertEqual(summary2["unchanged_this_run"], 3)
        self.assertEqual(summary2["pending_amazon_upload_count"], 0)
        self.assertEqual(list(delta_dir.iterdir()), [])

        # Hide p_003 (member of d_001 AND solo candidate) and update p_001 -> d_001 removed, only p_001 in delta!
        (self.album / "hidden.json").write_text(json.dumps(["p_003"]), encoding="utf-8")
        make_test_jpeg(self.album / "photos" / "p_001.jpg", size=(1280, 840), color=(70, 110, 155))

        summary3 = export_amazon_photos_bundle(
            album_dir=self.album,
            export_dir=export_dir,
            delta_dir=delta_dir,
            sync_state_path=state_path,
        )
        self.assertEqual(summary3["total_slides"], 2)
        self.assertEqual(summary3["updated_this_run"], 1)
        self.assertEqual(summary3["removed_this_run"], 1)
        self.assertEqual([p.name for p in delta_dir.iterdir()], ["p_001.jpg"])
        self.assertEqual(summary3["removed_from_amazon_album_files"], ["d_001.jpg"])

    def test_refresh_standalone_upgrades_originals_and_stages_cloud(self):
        usb_orig = self.base / "usb_originals"
        usb_orig.mkdir()
        # Provide a full-res 1800x1200 master for IMG_0001.HEIC (saved as .jpg) and IMG_0003
        make_test_jpeg(usb_orig / "IMG_0001.jpg", size=(1800, 1200), color=(50, 95, 145))
        make_test_jpeg(usb_orig / "IMG_0003.jpg", size=(1200, 1800), color=(75, 135, 95))

        key_file = self.base / "private" / "cloud_key"
        res = run_refresh(
            album_dir=self.album,
            usb_originals_dir=usb_orig,
            usb_daemon_dir=self.base / "no_daemon",
            amazon_export_dir=self.base / "amz_out",
            amazon_delta_dir=self.base / "amz_delta",
            amazon_sync_state=self.base / "amz_sync.json",
            cloud_stage_dir=self.base / "cloud_stage",
            cloud_sync_state=self.base / "cloud_sync.json",
            key_file=key_file,
            stage_cloud=True,
            push_cloud=False,
        )
        self.assertEqual(res["upgraded_originals"], 2)
        self.assertEqual(res["full_2048_photos"], 2)
        self.assertTrue(key_file.is_file())
        self.assertEqual(os.stat(key_file).st_mode & 0o777, 0o600)
        self.assertGreaterEqual(len(ensure_private_access_key(key_file)), 32)
        self.assertTrue((self.base / "cloud_stage" / "embeddings.f16.bin").is_file())
        self.assertTrue((self.base / "cloud_stage" / "manifest.json").is_file())

    def test_cloudflare_worker_auth_r2_control_and_embeddings_in_node(self):
        stage_dir = self.base / "cloud_stage"
        stage_cloud_bundle(
            album_dir=self.album,
            stage_dir=stage_dir,
            sync_state_path=self.base / "cloud_sync.json",
        )

        node_harness = """
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import worker from './cloudflare/worker.js';

const stageDir = process.env.TEST_STAGE_DIR;
const store = new Map();

function walk(dir, prefix = '') {
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const rel = prefix ? `${prefix}/${ent.name}` : ent.name;
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) walk(full, rel);
    else store.set(rel, fs.readFileSync(full));
  }
}
walk(stageDir);

const mockBucket = {
  async get(key) {
    if (!store.has(key)) return null;
    const buf = store.get(key);
    return {
      etag: 'etag-' + key,
      httpEtag: '"etag-' + key + '"',
      body: buf,
      async arrayBuffer() { return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength); },
      async json() { return JSON.parse(buf.toString('utf8')); },
    };
  },
  async put(key, val) {
    store.set(key, Buffer.from(typeof val === 'string' ? val : new Uint8Array(val)));
  },
};

const mockAssets = {
  async fetch(req) {
    const u = new URL(req.url);
    const localFile = path.join('./web', u.pathname.slice(1));
    if (fs.existsSync(localFile) && fs.statSync(localFile).isFile()) {
      return new Response(fs.readFileSync(localFile), { status: 200 });
    }
    return new Response('Not found', { status: 404 });
  },
};

const SECRET = 'super_secret_access_key_for_testing_1234567890';
const env = { FRAME_ACCESS_KEY: SECRET, ALBUM_BUCKET: mockBucket, ASSETS: mockAssets };

// 1. Fail closed when secret missing
const resNoSecret = await worker.fetch(new Request('https://frame.example.com/'), { FRAME_ACCESS_KEY: '' });
assert.equal(resNoSecret.status, 503);

// 2. robots.txt always disallows indexing
const resRobots = await worker.fetch(new Request('https://frame.example.com/robots.txt'), env);
assert.equal(resRobots.status, 200);
assert.match(await resRobots.text(), /Disallow: \\//);

// 3. Unauthenticated requests blocked with 401 + noindex
const resUnauthApi = await worker.fetch(new Request('https://frame.example.com/api/state'), env);
assert.equal(resUnauthApi.status, 401);
assert.match(resUnauthApi.headers.get('X-Robots-Tag') || '', /noindex/);

const resUnauthPhoto = await worker.fetch(new Request('https://frame.example.com/photos/p_001.jpg'), env);
assert.equal(resUnauthPhoto.status, 401);

const resUnauthHtml = await worker.fetch(new Request('https://frame.example.com/?autostart=1'), env);
assert.equal(resUnauthHtml.status, 401);
assert.match(await resUnauthHtml.text(), /Private Photo Frame/);

// 4. Key bootstrap sets __Host-frame_auth cookie & strips key param cleanly
const resBoot = await worker.fetch(
  new Request(`https://frame.example.com/?key=${SECRET}&autostart=1`),
  env
);
assert.equal(resBoot.status, 302);
assert.equal(resBoot.headers.get('Location'), '/?autostart=1');
const setCookie = resBoot.headers.get('Set-Cookie') || '';
assert.match(setCookie, /^__Host-frame_auth=[0-9a-f]{64};.*HttpOnly.*Secure/);
const cookiePair = setCookie.split(';')[0];

// 5. Authenticated HTML, /api/state, /photos/*, traversal protection, and 304 ETag
const resIndex = await worker.fetch(
  new Request('https://frame.example.com/?autostart=1', { headers: { Cookie: cookiePair } }),
  env
);
assert.equal(resIndex.status, 200);
assert.match(await resIndex.text(), /Start album/);

const resState = await worker.fetch(
  new Request('https://frame.example.com/api/state?role=display&slide=d_001', {
    headers: { Cookie: cookiePair, 'User-Agent': 'Mozilla/5.0 Silk/138 Echo' },
  }),
  env
);
const stateJson = await resState.json();
assert.equal(stateJson.curated, true);
assert.equal(stateJson.slides.length, 4);

const resPhoto = await worker.fetch(
  new Request('https://frame.example.com/photos/p_001.jpg', { headers: { Cookie: cookiePair } }),
  env
);
assert.equal(resPhoto.status, 200);
const etag = resPhoto.headers.get('ETag');
const res304 = await worker.fetch(
  new Request('https://frame.example.com/photos/p_001.jpg', {
    headers: { Cookie: cookiePair, 'If-None-Match': etag },
  }),
  env
);
assert.equal(res304.status, 304);

const resTrav = await worker.fetch(
  new Request('https://frame.example.com/photos/..%2Fmanifest.json', { headers: { Cookie: cookiePair } }),
  env
);
assert.equal(resTrav.status, 404);

// 6. Remote control channel switch + hide persisted into R2
const resCtrl = await worker.fetch(
  new Request('https://frame.example.com/api/control', {
    method: 'POST',
    headers: { Cookie: cookiePair, 'Content-Type': 'application/json' },
    body: JSON.stringify({ channel: 'family', interval_sec: 30 }),
  }),
  env
);
const ctrlJson = await resCtrl.json();
assert.equal(ctrlJson.state.active_channel, 'family');
assert.equal(ctrlJson.state.interval_sec, 30);
assert.equal(ctrlJson.state.slides.length, 1);
assert.equal(ctrlJson.state.slides[0].id, 'p_002');

// 7. Lexical search works with zero external API keys
const resLex = await worker.fetch(
  new Request('https://frame.example.com/api/search?q=alpine+powder', { headers: { Cookie: cookiePair } }),
  env
);
const lexJson = await resLex.json();
assert.equal(lexJson.mode, 'lexical_fallback');
assert.equal(lexJson.results[0].id, 'd_001');

// 8. Hiding p_003 also hides diptych d_001
const resHide = await worker.fetch(
  new Request('https://frame.example.com/api/hide', {
    method: 'POST',
    headers: { Cookie: cookiePair, 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: 'p_003', hidden: true }),
  }),
  env
);
const hideJson = await resHide.json();
assert.equal(hideJson.hidden_ids.includes('p_003'), true);

const resAlbum = await worker.fetch(
  new Request('https://frame.example.com/api/album', { headers: { Cookie: cookiePair } }),
  env
);
const albumJson = await resAlbum.json();
const dipSlide = albumJson.slides.find((s) => s.id === 'd_001');
assert.equal(dipSlide.hidden, true);
console.log('WORKER_TESTS_OK');
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", node_harness],
            cwd=str(ROOT),
            env={**os.environ, "TEST_STAGE_DIR": str(stage_dir)},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        self.assertIn("WORKER_TESTS_OK", proc.stdout)

    def test_es5_syntax_discipline_for_echo_show_silk(self):
        album_js = (ROOT / "web" / "album.js").read_text(encoding="utf-8")
        # Ensure no ES6 let/const/arrow/fetch crept into Silk's primary ES5 slideshow script
        for forbidden in ("const ", "let ", "=>", "fetch("):
            self.assertNotIn(forbidden, album_js, f"Forbidden non-ES5 token {forbidden!r} in web/album.js")

        for js_rel in ("web/album.js", "web/sw.js", "web/gallery.js", "web/remote.js", "cloudflare/worker.js"):
            chk = subprocess.run(
                ["node", "--check", str(ROOT / js_rel)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(chk.returncode, 0, msg=f"Syntax check failed for {js_rel}: {chk.stderr}")


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. EchoFrame; Section 7 attribution terms in ATTRIBUTION.md.
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import ProxyHandler, Request, build_opener

import numpy as np

from echoframe.serve_album import WEB, AlbumServer


class AlbumServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        (self.photos / "sample space.jpg").write_bytes(b"test-jpeg")
        (self.photos / "secret.txt").write_text("not a photo")
        (self.root / "outside.jpg").write_bytes(b"private")
        (self.photos / "linked.jpg").symlink_to(self.root / "outside.jpg")
        self.server = AlbumServer(("127.0.0.1", 0), self.photos)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def get(self, path):
        return self.opener.open(self.url + path, timeout=2)

    def post_json(self, path, payload):
        req = Request(
            self.url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self.opener.open(req, timeout=2)

    def test_manifest_and_photo(self):
        with self.get("/api/photos") as r:
            data = json.load(r)
        self.assertFalse(data["demo"])
        self.assertEqual(data["photos"], [{"src": "/photos/sample%20space.jpg", "label": "sample space"}])
        with self.get(data["photos"][0]["src"]) as r:
            self.assertEqual(r.read(), b"test-jpeg")
            self.assertEqual(r.headers["Content-Type"], "image/jpeg")

    def test_no_arbitrary_file_access(self):
        for path in [
            "/photos/../outside.jpg",
            "/photos/%2e%2e%2foutside.jpg",
            "/photos/linked.jpg",
            "/photos/secret.txt",
            "/echoframe/serve_album.py",
            "/photos/%2fetc%2fpasswd",
        ]:
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                self.get(path)
            self.assertEqual(error.exception.code, 404)

    def test_gallery_remote_navigation(self):
        evaluate_link = """
            const [page, href, script] = process.argv.slice(1);
            const location = new URL(page);
            const link = { href: new URL(href, location).href };
            const document = { getElementById: id => ({ "other-view": link })[id] };
            eval(script);
            process.stdout.write(link.href);
        """
        for page, target in [("gallery", "remote"), ("remote", "gallery")]:
            for url in [self.url + "/" + page, (WEB / (page + ".html")).as_uri()]:
                with self.subTest(url=url), self.opener.open(url) as response:
                    html = response.read().decode()
                    href = re.search(r'id="other-view" href="([^"]+)"', html)[1]
                    script = re.search(r"<script>(.*?)</script>", html, re.S)[1]
                    link = subprocess.check_output(
                        ["node", "-e", evaluate_link, url, href, script], text=True
                    )
                    expected = target + ".html" if url.endswith(".html") else target
                    self.assertEqual(link, urljoin(url, expected))
                    with self.opener.open(link) as destination:
                        self.assertEqual(destination.read(), (WEB / (target + ".html")).read_bytes())

    def test_assets_and_diagnostics(self):
        with self.get("/") as r:
            self.assertIn(b"Start album", r.read())
        with self.get("/gallery") as r:
            gallery_html = r.read()
            self.assertIn(b"Home Gallery", gallery_html)
            self.assertIn(b'id="search-error"', gallery_html)
        with self.get("/remote") as r:
            remote_html = r.read()
            self.assertIn(b"Frame Remote", remote_html)
            self.assertIn(b'id="now-playing"', remote_html)
        with self.get("/gallery.js") as r:
            gallery_js = r.read()
            self.assertIn(b"Search failed for", gallery_js)
        with self.get("/remote.js") as r:
            remote_js = r.read()
            self.assertIn(b"Frame status unavailable", remote_js)
        with self.get("/app.css") as r:
            app_css = r.read()
            self.assertIn(b"minmax(min(290px, 100%), 1fr)", app_css)
            self.assertIn(b"font-size: 16px;", app_css)
        with self.get("/api/ping") as r:
            self.assertTrue(json.load(r)["ok"])
        with self.get("/api/status") as r:
            self.assertEqual(len(json.load(r)["clients"]), 1)
        with self.get("/silence.wav") as r:
            self.assertTrue(r.read().startswith(b"RIFF"))
        with self.get("/sw.js") as r:
            self.assertIn(b"PRECACHE_SLIDES", r.read())
            self.assertIn("noindex", r.headers.get("X-Robots-Tag", ""))
        with self.get("/robots.txt") as r:
            self.assertIn(b"Disallow: /", r.read())

    def test_demo_without_photos(self):
        self.server.photos = None
        with self.get("/api/photos") as r:
            self.assertEqual(json.load(r), {"demo": True, "photos": []})


class CuratedAlbumServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.album = self.root / "album"
        (self.album / "photos").mkdir(parents=True)
        (self.album / "diptychs").mkdir(parents=True)
        (self.album / "photos" / "p_001.jpg").write_bytes(b"single-jpg")
        (self.album / "diptychs" / "d_001.jpg").write_bytes(b"diptych-jpg")
        (self.root / "outside.jpg").write_bytes(b"secret")
        (self.album / "diptychs" / "sym.jpg").symlink_to(self.root / "outside.jpg")

        manifest = {
            "version": 2,
            "channels": [
                {"id": "smart_frame", "name": "Ambient Mix", "description": "Default"},
                {"id": "featured", "name": "Featured", "description": "Top"},
                {"id": "family", "name": "Family & Friends", "description": "Family"},
                {"id": "diptychs", "name": "Portrait Diptychs", "description": "Pairs"},
            ],
            "stats": {"curated_photos": 1, "diptych_slides": 1},
            "photos": [{"id": "p_001", "src": "/photos/p_001.jpg", "resolution_tier": "thumb_640"}],
            "slides": [
                {
                    "id": "d_001",
                    "kind": "diptych",
                    "member_ids": ["p_001", "p_002"],
                    "src": "/diptychs/d_001.jpg",
                    "display_caption": "Snowy alpine ski ridge",
                    "date_label": "Feb 2025",
                    "age_badge": "",
                    "accent_rgb": [40, 58, 82],
                    "channels": ["featured", "diptychs"],
                    "tags": ["ski", "snow", "mountain"],
                    "in_default_frame": True,
                    "frame_score": 9.1,
                },
                {
                    "id": "p_001",
                    "photo_id": "p_001",
                    "kind": "single",
                    "src": "/photos/p_001.jpg",
                    "display_caption": "Warm morning smile",
                    "date_label": "Oct 2026",
                    "age_badge": "6 mos",
                    "accent_rgb": [92, 74, 54],
                    "channels": ["family"],
                    "tags": ["baby", "smile"],
                    "in_default_frame": True,
                    "frame_score": 8.4,
                },
            ],
        }
        (self.album / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        vecs = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        np.savez_compressed(self.album / "album_embeddings.npz", ids=np.array(["d_001", "p_001"]), vectors=vecs)

        # Point gateway to unreachable port to verify clean lexical fallback when offline
        self.server = AlbumServer(("127.0.0.1", 0), self.album, gateway_url="http://127.0.0.1:9")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def get(self, path):
        return self.opener.open(self.url + path, timeout=2)

    def post_json(self, path, payload):
        req = Request(
            self.url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self.opener.open(req, timeout=2)

    def test_curated_state_control_hide_and_diptych_security(self):
        with self.get("/api/state?role=display&slide=d_001") as r:
            st = json.load(r)
        self.assertTrue(st["curated"])
        self.assertEqual(len(st["slides"]), 2)

        with self.get("/diptychs/d_001.jpg") as r:
            self.assertEqual(r.read(), b"diptych-jpg")

        for bad in ["/diptychs/../outside.jpg", "/diptychs/sym.jpg"]:
            with self.assertRaises(HTTPError) as err:
                self.get(bad)
            self.assertEqual(err.exception.code, 404)

        # Channel switch + interval update via /api/control
        with self.post_json("/api/control", {"channel": "family", "interval_sec": 30}) as r:
            ctrl = json.load(r)
        self.assertEqual(ctrl["state"]["active_channel"], "family")
        self.assertEqual(ctrl["state"]["interval_sec"], 30)
        self.assertEqual([s["id"] for s in ctrl["state"]["slides"]], ["p_001"])

        # Lexical fallback search works even when local OpenAI gateway is unreachable
        with self.get("/api/search?q=alpine+ski") as r:
            sres = json.load(r)
        self.assertEqual(sres["results"][0]["id"], "d_001")

        # Hiding p_001 also hides diptych d_001 because p_001 is a member of d_001
        with self.post_json("/api/hide", {"id": "p_001", "hidden": True}) as r:
            hres = json.load(r)
        self.assertEqual(hres["state"]["slides"], [])


if __name__ == "__main__":
    unittest.main()

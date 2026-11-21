import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from PIL import Image

from scripts.deploy_webos_tv import (
    export_tv_bundle,
    generate_screensaver_photo_list_js,
    render_tv_1080p_jpeg,
)


class TestWebOSTVDeploy(unittest.TestCase):
    def test_render_tv_1080p_jpeg_baseline_and_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, size, kind in (
                ("wide.jpg", (2048, 1152), "single"),
                ("portrait.jpg", (1200, 1800), "single"),
                ("diptych.jpg", (1600, 960), "diptych"),
            ):
                p = root / name
                Image.new("RGB", size, (120, 90, 60)).save(p, format="JPEG", quality=90)
                raw = render_tv_1080p_jpeg(p, [120, 90, 60], kind=kind)
                with Image.open(io.BytesIO(raw)) as out:
                    self.assertEqual(out.size, (1920, 1080))
                    self.assertFalse(out.info.get("progressive", False))

    def test_export_tv_bundle_and_tv_server_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            album_dir = root / "album"
            (album_dir / "photos").mkdir(parents=True)
            (album_dir / "diptychs").mkdir(parents=True)

            Image.new("RGB", (1600, 1066), (80, 110, 140)).save(
                album_dir / "photos" / "p_one.jpg", format="JPEG", quality=90
            )
            Image.new("RGB", (1600, 960), (140, 100, 80)).save(
                album_dir / "diptychs" / "d_two.jpg", format="JPEG", quality=90
            )

            manifest = {
                "channels": [
                    {"id": "smart_frame", "name": "Ambient Mix", "description": "Default"},
                    {"id": "travel_outdoors", "name": "Mountains & Travel", "description": "Travel"},
                ],
                "slides": [
                    {
                        "id": "p_one",
                        "kind": "single",
                        "src": "/photos/p_one.jpg",
                        "rel_path": "photos/p_one.jpg",
                        "display_caption": "Alpine morning light",
                        "date_label": "Jul 2025",
                        "channels": ["travel_outdoors"],
                        "accent_rgb": [80, 110, 140],
                        "in_default_frame": True,
                    },
                    {
                        "id": "d_two",
                        "kind": "diptych",
                        "src": "/diptychs/d_two.jpg",
                        "rel_path": "diptychs/d_two.jpg",
                        "display_caption": "Warm family dinner",
                        "date_label": "Oct 2025",
                        "channels": ["featured"],
                        "accent_rgb": [140, 100, 80],
                        "in_default_frame": False,
                    },
                ],
            }
            (album_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            stage_dir = root / "stage"
            summary = export_tv_bundle(album_dir=album_dir, stage_dir=stage_dir)
            self.assertEqual(summary["total_slides"], 2)
            self.assertEqual(summary["ambient_slides"], 1)

            ipk_bytes = Path(summary["ipk_path"]).read_bytes()
            self.assertTrue(ipk_bytes.startswith(b"!<arch>\n"))

            qml_ss = (stage_dir / "qml" / "ScreenSaver.qml").read_text(encoding="utf-8")
            self.assertIn("Alpine morning light", qml_ss)
            self.assertNotIn("/*__FALLBACK_SLIDES__*/[]", qml_ss)

            photo_list_js = generate_screensaver_photo_list_js(["p_one.jpg"])
            self.assertIn("/media/developer/echoframe/slides/", photo_list_js)
            self.assertIn("p_one.jpg", photo_list_js)

            # Verify tv_server.js serves /api/state, /api/control, and /slides/p_one.jpg
            repo_root = Path(__file__).resolve().parents[1]
            tv_server_js = repo_root / "webos" / "tv_server.js"
            node_script = (
                "const http = require('http');"
                f"const {{ startServer }} = require({json.dumps(str(tv_server_js))});"
                f"const srv = startServer({{ port: 0, host: '127.0.0.1', baseDir: {json.dumps(str(stage_dir))} }});"
                "srv.on('listening', () => {"
                "  const port = srv.address().port;"
                "  const req = (m, p, b) => new Promise((res, rej) => {"
                "    const r = http.request({ host: '127.0.0.1', port, path: p, method: m,"
                "      headers: b ? { 'Content-Type': 'application/json' } : {} }, (resp) => {"
                "      const chunks = []; resp.on('data', d => chunks.push(d));"
                "      resp.on('end', () => res({ s: resp.statusCode, b: Buffer.concat(chunks) }));"
                "    }); r.on('error', rej); if (b) r.write(JSON.stringify(b)); r.end();"
                "  });"
                "  Promise.resolve().then(async () => {"
                "    const st1 = JSON.parse((await req('GET', '/api/state?role=screensaver')).b.toString());"
                "    if (st1.slides.length !== 1 || st1.slides[0].id !== 'p_one') throw new Error('bad state');"
                "    const img = await req('GET', st1.slides[0].src);"
                "    if (img.s !== 200 || img.b.length < 1000) throw new Error('bad slide');"
                "    const ctrl = JSON.parse((await req('POST', '/api/control', { interval_sec: 30, show_clock: false })).b.toString());"
                "    if (ctrl.state.interval_sec !== 30 || ctrl.state.show_clock !== false) throw new Error('bad control');"
                "    const srch = JSON.parse((await req('GET', '/api/search?q=alpine')).b.toString());"
                "    if (srch.results.length !== 1) throw new Error('bad search');"
                "    srv.close();"
                "  }).catch(e => { console.error(e); srv.close(); process.exit(1); });"
                "});"
            )
            res = subprocess.run(
                ["node", "-e", node_script],
                env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "ECHOFRAME_NO_AUTOSTART": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)


if __name__ == "__main__":
    unittest.main()

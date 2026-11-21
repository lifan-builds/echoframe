# EchoFrame

Make your Echo Show worth keeping on the counter again — turning thousands of buried iPhone photos into a calm, ad-free, **AI-native ambient photo album** that runs **24/7 with your Mac closed and your iPhone unplugged**.

- **Overview & Interactive Architecture:** [https://clifan.com/echoframe/](https://clifan.com/echoframe/)
- **Echo Show Vega OS & Fire OS Ad Analysis:** [`docs/echo-show-ad-architecture.md`](docs/echo-show-ad-architecture.md)

---

## The Story Behind EchoFrame

Most households have thousands of great photos sitting unread inside an iPhone camera roll—dinners with friends, dogs, mountain trips, family milestones—and an Echo Show sitting in the kitchen or entryway that mostly shows cooking timers and full-screen "Sponsored" ads for dish sponges.

Even dedicated $200 Wi-Fi frames go stale because nobody keeps up with manually dragging photos into an album every month, while dumping a raw camera roll onto a horizontal screen surfaces parking receipts, screenshots, 15-shot burst duplicates, and vertical 9:16 portraits chopped across people's foreheads.

**EchoFrame** fixes both halves—acting like a thoughtful gallery editor over your raw iPhone library and reclaiming Echo Show hardware so it stays quiet and useful:

1. **AI-Native Curation Over Your Raw iPhone Library (`scripts/extract_iphone_album.py` + `swift/USBCameraBridge.swift`):**
   - Scans any exported photo folder (`--source-dir`) or reads directly from a USB-connected iPhone on macOS via `ImageCaptureCore` (strictly read-only, zero iCloud required).
   - Scores every shot with OpenAI Vision to drop receipts, screenshots, clutter, closed eyes, and private/awkward frames while surfacing food, pets, family, travel, and quiet scenery.
   - Deduplicates bursts (`64-bit dHash` + 2-hour/daily caps) and paces memories across every year and season in your archive so one recent trip never drowns out five years of life.
2. **Zero-Crop `1600×960` (`16:10`) Museum Diptychs:**
   - Detects `focal_xy` face/subject coordinates and pairs vertical iPhone portraits that share color mood (`warm_gold`, `alpine_cool`, `lush_green`, `soft_neutral`, `vibrant`) and capture windows side by side on a `#0b0e13` + dominant-accent matte—so `1024×600` and `960×480` screens never clip foreheads.
3. **Search & Cast by Memory (`text-embedding-3-large`):**
   - Indexes every slide semantically so you can open `/remote` or `/gallery` on your phone, type *"snowy mountain ski trip"* or *"shared hotpot dinner"*, and cast that exact mood across your home displays.
4. **Reclaiming Echo Show From "Sponsored" Ads (`scripts/configure_alexa_ad_free.py` + [`docs/echo-show-ad-architecture.md`](docs/echo-show-ad-architecture.md)):**
   - Reverse-engineered from Vega OS 1.3 (`hypnos`) and Fire OS 5.5 (`knight`): sinkholes the 4 outbound `Juno` ad-fill beacons (`advertising.amazon.dev`, `jet.amazon`, `amazon-adsystem.com`, `unagi-na.amazon.com`) via Echo Show's built-in Wi-Fi `DNS 1 / DNS 2` or NextDNS while keeping Alexa voice, timers, and `m.media-amazon.com` photos working, paired with rolling 3-hour `"Start Photo Frame"` routines and lossless EXIF-dated `2048px` sync (`~390` slides = `~286 MB`, `< 6%` of Amazon Photos' free 5 GB tier).

---

## Quick Start

### 1. Install Dependencies

```bash
git clone https://github.com/lifan-builds/echoframe.git
cd echoframe
pip install pillow numpy pytest
export OPENAI_API_KEY="sk-..."
```

### 2. Curate Your iPhone Photos & Render `16:10` Diptychs

Point EchoFrame at any exported folder of iPhone photos (HEIC/JPEG/PNG/WebP):

```bash
python3 scripts/extract_iphone_album.py --source-dir ~/Pictures/iPhone-Export
```

*(Optional macOS direct USB mode — zero iCloud required: compile the read-only `ImageCaptureCore` daemon with `swiftc -O swift/USBCameraBridge.swift -o data/bin/USBCameraBridge`, connect and unlock your iPhone, and run `./data/bin/USBCameraBridge ./data/usb_daemon` to pull `2048px` masters automatically.)*

### 3. Export for Native 24/7 Echo Show Playback (Zero Mac Open)

```bash
python3 scripts/export_amazon_photos.py --reveal
```

Upload the deduplicated `16:10` JPEGs in `data/export/amazon_photos_ambient_mix/` to an Amazon Photos album (e.g. **`Ambient Mix`**) and bind your Echo Show(s) to that album at `https://www.amazon.com/photos/device-personalization`. Future runs stage only changed JPEGs in `data/export/amazon_photos_delta/`.

### 4. Silence Echo Show "Sponsored" Ads

See [`docs/echo-show-ad-architecture.md`](docs/echo-show-ad-architecture.md) for the full Vega OS / Fire OS breakdown:
1. **Sinkhole the 4 Juno ad-fill domains** (`advertising.amazon.dev`, `jet.amazon`, `amazon-adsystem.com`, `unagi-na.amazon.com`) on a dedicated NextDNS / Pi-hole profile—either per-Echo via *Settings -> Network -> Forget Wi-Fi -> Rejoin -> See Advanced Options -> IP Settings: Static -> DNS 1 / DNS 2* or on your router—while leaving `m.media-amazon.com`, `avs-alexa-*.amazon.com`, and `ntp-g7g.amazon.com` unblocked.
2. **Automate 3-hour `"Start Photo Frame"` routines** across your Echo Shows (via the Alexa app or `scripts/configure_alexa_ad_free.py --apply-routines`).

### 5. Optional: Local LAN Server or Private Cloudflare R2 Web Frame

```bash
# Serve /, /gallery, and /remote over home Wi-Fi:
WIFI_IP=$(ipconfig getifaddr en0)
python3 scripts/serve_album.py --host "$WIFI_IP" --port 8765
```

| Route | Purpose |
| :--- | :--- |
| `http://<WIFI_IP>:8765/?autostart=1` | **Ambient Display** — ES5 dual-buffer crossfade, `1600×960` diptychs, dominant-color radial glow, offline `sw.js` cache |
| `http://<WIFI_IP>:8765/gallery` | **Semantic Gallery** — Natural-language `text-embedding-3-large` search, channel tabs, one-click cast & hide |
| `http://<WIFI_IP>:8765/remote` | **Pocket Remote** — Transport (`Prev` / `Pause` / `Next`), prompt casting, cadence (`10s / 30s / 60s / 5m`), live display heartbeats |

To deploy an always-on private edge mirror on Cloudflare Workers + R2 (`$0/mo` free tier), copy `wrangler.jsonc.example` to `wrangler.jsonc` and run `python3 scripts/stage_cloud_bundle.py --generate-key` + `--push`.

### 6. Optional: Rooted LG webOS TV Idle Screensaver, No Signal Gallery & TV App

If you have a rooted LG webOS TV (webOS 5+ with Homebrew / Telnet), you can deploy all curated slides as `1920×1080` `16:9` baseline JPEGs directly onto TV internal storage (`/media/developer/echoframe`) for 24/7 offline playback with zero Mac required:

```bash
python3 scripts/deploy_webos_tv.py --tv-ip <TV_IP>
```

- **System Idle Screensaver (`com.webos.app.screensaver`):** Boot-persistent `mount --bind` QML overlay with dual-`Image` crossfades, dominant-color matte, captions, and clock pill.
- **No Signal HDMI Screen (`com.webos.app.inputcommon`):** Full-screen `16:9` `Ambient Mix` photo slideshow on unused inputs.
- **Launchable `EchoFrame` TV App (`com.echoframe.webos`) + LAN Remote:** Local Node.js server on port `18663` serving `/`, `/gallery`, and `/remote` over home Wi-Fi.

---

## Repository Layout

| Path | Description |
| :--- | :--- |
| `scripts/extract_iphone_album.py` | AI Vision safety/aesthetic pass, temporal & `dHash` deduplication, `focal_xy` `1600×960` portrait diptych renderer, and `text-embedding-3-large` indexer |
| `scripts/export_amazon_photos.py` | Lossless EXIF `DateTimeOriginal` injector, solo-portrait `16:10` gallery matter, and SHA-256 incremental delta exporter |
| `scripts/configure_alexa_ad_free.py` | Alexa `behaviors/automations` 3-hour Photo Frame scheduler, live preview trigger, and DNS sinkhole verifier |
| `scripts/serve_album.py` | Zero-dependency multi-device HTTP server (`/`, `/gallery`, `/remote`, semantic/lexical search, display heartbeats) |
| `scripts/deploy_webos_tv.py` + `webos/` | Standalone LG webOS TV exporter/deployer (QML idle screensaver, No Signal full-screen gallery, local `:18663` server, and `com.echoframe.webos` `.ipk` app) |
| `scripts/stage_cloud_bundle.py` + `cloudflare/worker.js` | Private 256-bit token-gated Cloudflare Worker + `float16` edge vector search + Private R2 sync |
| `scripts/instant_share.py` + `whisperlink_info.py` | Account-free Echo Show Gen 1 Silk Instant Share J-PAKE launcher (`NIST-3072`) & read-only WhisperLink discovery |
| `swift/USBCameraBridge.swift` | Read-only macOS `ImageCaptureCore` iPhone USB catalog, `640px` thumbnail, and `2048px` master downloader |

## Verification

```bash
pytest -q
node --check web/album.js
node --check web/sw.js
node --check web/gallery.js
node --check web/remote.js
node --check cloudflare/worker.js
node --check webos/tv_server.js
```

## License

MIT © [Lifan Chen](https://clifan.com)

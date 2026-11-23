# EchoFrame

Make your Echo Show worth keeping on the counter again — turning thousands of buried iPhone photos into a calm, ad-free, **AI-native ambient photo album** that runs **24/7 with your Mac closed and your iPhone unplugged**.

- **Overview & Interactive Architecture:** [https://clifan.com/echoframe/](https://clifan.com/echoframe/)
- **Echo Show Vega OS & Fire OS Ad Analysis:** [`docs/echo-show-ad-architecture.md`](docs/echo-show-ad-architecture.md)

---

## The Story Behind EchoFrame

Most households have thousands of great photos sitting unread inside an iPhone camera roll—dinners with friends, dogs, mountain trips, family milestones—and an Echo Show sitting in the kitchen or entryway that mostly shows cooking timers and full-screen "Sponsored" ads for dish sponges.

Even dedicated $200 Wi-Fi frames go stale because nobody keeps up with manually dragging photos into an album every month, while dumping a raw camera roll onto a horizontal screen surfaces parking receipts, screenshots, 15-shot burst duplicates, and vertical 9:16 portraits chopped across people's foreheads.

**EchoFrame** fixes both halves—acting like a thoughtful gallery editor over your raw iPhone library and reclaiming Echo Show hardware so it stays quiet and useful:

1. **AI-Native Curation Over Your Raw iPhone Library (`echoframe curate` + `swift/USBCameraBridge.swift`):**
   - Scans any exported photo folder (`--source-dir`) or reads directly from a USB-connected iPhone on macOS via `ImageCaptureCore` (strictly read-only, zero iCloud required).
   - Scores every shot with OpenAI Vision to drop receipts, screenshots, clutter, closed eyes, and private/awkward frames while surfacing food, pets, family, travel, and quiet scenery.
   - Deduplicates bursts (`64-bit dHash` + 2-hour/daily caps) and paces memories across every year and season in your archive so one recent trip never drowns out five years of life.
2. **Zero-Crop `1600×960` (`16:10`) Museum Diptychs:**
   - Detects `focal_xy` face/subject coordinates and pairs vertical iPhone portraits that share color mood (`warm_gold`, `alpine_cool`, `lush_green`, `soft_neutral`, `vibrant`) and capture windows side by side on a `#0b0e13` + dominant-accent matte—so `1024×600` and `960×480` screens never clip foreheads.
3. **Search & Cast by Memory (`text-embedding-3-large`):**
   - Indexes every slide semantically so you can open `/remote` or `/gallery` on your phone, type *"snowy mountain ski trip"* or *"shared hotpot dinner"*, and cast that exact mood across your home displays.
4. **Reclaiming Echo Show From "Sponsored" Ads (`echoframe ad-check` + [`docs/echo-show-ad-architecture.md`](docs/echo-show-ad-architecture.md)):**
   - Reverse-engineered from Vega OS 1.3 (`hypnos`) and Fire OS 5.5 (`knight`): sinkholes the 4 outbound `Juno` ad-fill beacons (`advertising.amazon.dev`, `jet.amazon`, `amazon-adsystem.com`, `unagi-na.amazon.com`) via Echo Show's built-in Wi-Fi `DNS 1 / DNS 2` or NextDNS while keeping Alexa voice, timers, and `m.media-amazon.com` photos working, paired with rolling 3-hour `"Start Photo Frame"` routines and lossless EXIF-dated `2048px` sync (`~390` slides = `~286 MB`, `< 6%` of Amazon Photos' free 5 GB tier).

---

## Quick Start

### 1. Run EchoFrame With `uvx` (No Clone)

EchoFrame installs straight from GitHub (the `echoframe` name on PyPI belongs to an unrelated project, so always use `--from git+...`):

```bash
alias echoframe='uvx --from git+https://github.com/lifan-builds/echoframe echoframe'
echoframe --help
```

`echoframe` reads and writes `data/` (curated album + exports) and `private/profile.json` in the directory you run it from.

### 2. Check Whether Echo Show Ads Are Already Blocked (Zero API Keys)

```bash
echoframe ad-check                        # probe your system resolver
echoframe ad-check --dns 192.168.1.1      # probe your router / Pi-hole / NextDNS IP
```

Passes when the 4 Juno ad-fill beacons (`advertising.amazon.dev`, `jet.amazon`, `amazon-adsystem.com`, `unagi-na.amazon.com`) resolve to `0.0.0.0`/NXDOMAIN while `m.media-amazon.com` and `avs-alexa-*.amazon.com` still resolve; exits `1` otherwise (`--json` for the full report). No Amazon login or OpenAI key needed; only `dig` must be installed.

### 3. Curate Your iPhone Photos & Render `16:10` Diptychs

Point EchoFrame at any exported folder of iPhone photos (HEIC/JPEG/PNG/WebP):

```bash
export OPENAI_API_KEY="sk-..."
echoframe curate --source-dir ~/Pictures/iPhone-Export
```

*(Optional macOS direct USB mode — zero iCloud required: from a clone, compile the read-only `ImageCaptureCore` daemon with `swiftc -O swift/USBCameraBridge.swift -o data/bin/USBCameraBridge`, connect and unlock your iPhone, and run `./data/bin/USBCameraBridge ./data/usb_daemon` to pull `2048px` masters automatically.)*

### 4. Export for Native 24/7 Echo Show Playback (Zero Mac Open)

```bash
echoframe export --reveal
```

Upload the deduplicated `16:10` JPEGs in `data/export/amazon_photos_ambient_mix/` to an Amazon Photos album (e.g. **`Ambient Mix`**) and bind your Echo Show(s) to that album at `https://www.amazon.com/photos/device-personalization`. Future runs stage only changed JPEGs in `data/export/amazon_photos_delta/`.

### 5. Silence Echo Show "Sponsored" Ads

See [`docs/echo-show-ad-architecture.md`](docs/echo-show-ad-architecture.md) for the full Vega OS / Fire OS breakdown:
1. **Sinkhole the 4 Juno ad-fill domains** (`advertising.amazon.dev`, `jet.amazon`, `amazon-adsystem.com`, `unagi-na.amazon.com`) on a dedicated NextDNS / Pi-hole profile—either per-Echo via *Settings -> Network -> Forget Wi-Fi -> Rejoin -> See Advanced Options -> IP Settings: Static -> DNS 1 / DNS 2* or on your router—while leaving `m.media-amazon.com`, `avs-alexa-*.amazon.com`, and `ntp-g7g.amazon.com` unblocked.
2. **Automate 3-hour `"Start Photo Frame"` routines** across your Echo Shows (via the Alexa app or `python3 -m echoframe.configure_alexa_ad_free --apply-routines` from a clone). Re-run `echoframe ad-check` to confirm.

### 6. Optional: Local LAN Server or Private Cloudflare R2 Web Frame

These need the web/webOS/Worker assets, so run them from a clone (`git clone https://github.com/lifan-builds/echoframe.git && cd echoframe && uv sync`):

```bash
# Serve /, /gallery, and /remote over home Wi-Fi:
WIFI_IP=$(ipconfig getifaddr en0)
uv run python -m echoframe.serve_album --host "$WIFI_IP" --port 8765
```

| Route | Purpose |
| :--- | :--- |
| `http://<WIFI_IP>:8765/?autostart=1` | **Ambient Display** — ES5 dual-buffer crossfade, `1600×960` diptychs, dominant-color radial glow, offline `sw.js` cache |
| `http://<WIFI_IP>:8765/gallery` | **Semantic Gallery** — Natural-language `text-embedding-3-large` search, channel tabs, one-click cast & hide |
| `http://<WIFI_IP>:8765/remote` | **Pocket Remote** — Transport (`Prev` / `Pause` / `Next`), prompt casting, cadence (`10s / 30s / 60s / 5m`), live display heartbeats |

To deploy an always-on private edge mirror on Cloudflare Workers + R2 (`$0/mo` free tier), copy `wrangler.jsonc.example` to `wrangler.jsonc` and run `uv run python -m echoframe.stage_cloud_bundle --generate-key` + `--push`.

### 7. Optional: Rooted LG webOS TV Idle Screensaver, No Signal Gallery & TV App

If you have a rooted LG webOS TV (webOS 5+ with Homebrew / Telnet), you can deploy all curated slides as `1920×1080` `16:9` baseline JPEGs directly onto TV internal storage (`/media/developer/echoframe`) for 24/7 offline playback with zero Mac required:

```bash
uv run python -m echoframe.deploy_webos_tv --tv-ip <TV_IP>
```

- **System Idle Screensaver (`com.webos.app.screensaver`):** Boot-persistent `mount --bind` QML overlay with dual-`Image` crossfades, dominant-color matte, captions, and clock pill.
- **No Signal HDMI Screen (`com.webos.app.inputcommon`):** Full-screen `16:9` `Ambient Mix` photo slideshow on unused inputs.
- **Launchable `EchoFrame` TV App (`com.echoframe.webos`) + LAN Remote:** Local Node.js server on port `18663` serving `/`, `/gallery`, and `/remote` over home Wi-Fi.

---

## Repository Layout

| Path | Description |
| :--- | :--- |
| `echoframe/cli.py` | `echoframe` console entrypoint: `ad-check`, `curate`, `export` |
| `echoframe/extract_iphone_album.py` | AI Vision safety/aesthetic pass, temporal & `dHash` deduplication, `focal_xy` `1600×960` portrait diptych renderer, and `text-embedding-3-large` indexer |
| `echoframe/export_amazon_photos.py` | Lossless EXIF `DateTimeOriginal` injector, solo-portrait `16:10` gallery matter, and SHA-256 incremental delta exporter |
| `echoframe/configure_alexa_ad_free.py` | Alexa `behaviors/automations` 3-hour Photo Frame scheduler, live preview trigger, and DNS sinkhole verifier |
| `echoframe/serve_album.py` | Zero-dependency multi-device HTTP server (`/`, `/gallery`, `/remote`, semantic/lexical search, display heartbeats) |
| `echoframe/deploy_webos_tv.py` + `webos/` | Standalone LG webOS TV exporter/deployer (QML idle screensaver, No Signal full-screen gallery, local `:18663` server, and `com.echoframe.webos` `.ipk` app) |
| `echoframe/stage_cloud_bundle.py` + `cloudflare/worker.js` | Private 256-bit token-gated Cloudflare Worker + `float16` edge vector search + Private R2 sync |
| `echoframe/instant_share.py` + `whisperlink_info.py` | Account-free Echo Show Gen 1 Silk Instant Share J-PAKE launcher (`NIST-3072`) & read-only WhisperLink discovery |
| `swift/USBCameraBridge.swift` | Read-only macOS `ImageCaptureCore` iPhone USB catalog, `640px` thumbnail, and `2048px` master downloader |

## Verification

```bash
uv run --group dev pytest -q
node --check web/album.js
node --check web/sw.js
node --check web/gallery.js
node --check web/remote.js
node --check cloudflare/worker.js
node --check webos/tv_server.js
```

## License

Open source under [AGPL-3.0-only](https://github.com/lifan-builds/echoframe/blob/main/LICENSE), with the Section 7 attribution terms in [ATTRIBUTION.md](https://github.com/lifan-builds/echoframe/blob/main/ATTRIBUTION.md). Copies and modified versions must keep this credit in their README and `--version` or About output, mark their changes, and may not use the EchoFrame name:

> Based on EchoFrame by lifan-builds (https://github.com/lifan-builds/echoframe)

Copyright © 2026 [lifan-builds](https://github.com/lifan-builds). Pull requests are welcome under [CONTRIBUTING.md](https://github.com/lifan-builds/echoframe/blob/main/CONTRIBUTING.md) with a one-time [CLA](https://github.com/lifan-builds/echoframe/blob/main/CLA.md). For licensing outside AGPL, open an issue.

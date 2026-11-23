# How Echo Show "Sponsored" Ads Actually Work (Vega OS & Fire OS Analysis)

Static analysis of shipped Echo Show 5 Gen 3 **Vega OS 1.3** (`com.amazon.hypnos.kepler.os`) `.vpkg` packages (`homelauncher.vpkg`, `ambienthomekepler_armv7.vpkg`, `vegaspotlight_armv7.vpkg`, `KeplerSettingsApp.vpkg`, `SlateEcho.vpkg`) and Echo Show Gen 1 **Fire OS 5.5** (`com.amazon.knight.android.os`) explains why common ad-blocking advice fails—and how to cleanly silence Sponsored cards without breaking Alexa voice or Amazon Photos.

---

## 1. The Two-Stage `Juno` Ad Loop (`JunoAhsAdCard` / `AlexaJunoSafariContentProvider`)

Inside `ambienthomekepler_armv7/bundle/index.hermes.bundle` (Hermes v94 bytecode) and `homelauncher/lib/armv7/libhomelauncher.so`:

1. **Inbound ad payloads ride on core Alexa channels:**
   - Sponsored slides (`contentType: "ADS"`, `providerId: "AlexaJunoSafariContentProvider"`, `JunoAhsAdCard`) are stored in the on-device `DataStoreDaemon` (`keyNameSpace: "ADS"`) over the encrypted AVS directive stream (`avs-alexa-*.amazon.com`), and product creative images are served from **`m.media-amazon.com`**—the exact same CDN that serves your personal Amazon Photos JPEGs and weather icons.
   - Blocking `avs-alexa-*.amazon.com` or `m.media-amazon.com` breaks voice commands and your photo slideshow.
2. **Outbound ad-fill beacons are separate and sinkholeable:**
   - Whenever an idle photo or fallback card has an unserved ad slot, `ambientStore.sendUnservedImpression` fires an HTTPS beacon (`junoAdUnservedImpression` with `displayState=NEAR|FAR`) to:
     - `https://trck.ahs.*.turntable.sonic.advertising.amazon.dev`
     - `https://prod.na.jet.amazon` (plus regional `*.jet.amazon`)
   - Sinkholing just **four domains** to `0.0.0.0` at the DNS layer starves Juno's ad-fill trigger while keeping AVS voice, timers, Spotify, clock NTP (`ntp-g7g.amazon.com`), and Amazon Photos (`m.media-amazon.com`) 100% functional:
     ```text
     advertising.amazon.dev
     jet.amazon
     amazon-adsystem.com
     unagi-na.amazon.com
     ```

---

## 2. Why Popular Community Workarounds Fail or Break Your Photos

| Workaround | What Actually Happens in Firmware |
| :--- | :--- |
| **Enable Amazon Kids (`CHILD_DIRECTED`)** | In `ambienthome` (`filterKidsContent`), `OpModeStates.CHILD_DIRECTED` filters the ambient card deck down to `instanceof ClockCardV2 \|\| instanceof AplClockCard` (`"useAmbientStore: Kids session - kept 1 clock card of N card(s)"`), **deleting `PhotoCard-AmazonPhotos` along with `ADS`**. |
| **Schedule Night Mode 24/7** | In `homelauncher/assets/raw/screensaver.json` (`ToggleNightMode`), `nightMode: true` animates `backgroundImage.opacity` to `0`, hiding your photos completely behind a dim clock. |
| **Keep Silk Browser Open 24/7** | In `SlateEcho/manifest.toml` (`com.amazon.cloud9.main`), Vega OS sets `timeout-secs = 600`; any voice focus preemption, timer card, or overnight OTA reboot returns the display to the native launcher. |
| **Router HTTP "Keyword / Site Block"** | Consumer router HTTP site blockers only RST port-80 plaintext `Host` headers; `trck.ahs...` and `prod.na.jet.amazon` use HTTPS (`443`), so blocking must happen at **DNS** (`0.0.0.0` / `NXDOMAIN`). |
| **Single Daily `"Shuffle My Photos"` Routine** | Amazon's `"Start Photo Frame"` session is capped at **3 hours**, and `"shuffle/show my photos"` opens interactive photo browsing rather than dedicated Photo Frame mode. |

---

## 3. Two Clean Deployment Layers

### Layer A — DNS Sinkhole (Per-Device Echo Wi-Fi DNS or NextDNS / Pi-hole)

Contrary to blogs claiming Echo devices lack per-device DNS settings, `KeplerSettingsApp/assets/raw/network-doc.json` (`saveWifiConfiguration` -> `Network:SaveToConnect`) exposes `ipAddressEntered`, `gatewayEntered`, `networkPrefixEntered`, **`dns1Entered` (`DNS 1`)**, and **`dns2Entered` (`DNS 2`)** directly on the Echo Show's Wi-Fi screen:

1. Create a free NextDNS (or Pi-hole / AdGuard Home) profile with **zero generic blocklists** (zero false positives) and add only the 4 domains above (`advertising.amazon.dev`, `jet.amazon`, `amazon-adsystem.com`, `unagi-na.amazon.com`) to its Denylist.
2. Either set those DNS servers on your router **or** scope them strictly to your Echo Show(s) on-screen:
   - *Settings -> Network -> Forget current Wi-Fi -> Rejoin -> **See Advanced Options** -> **IP Settings: Static*** -> enter `DNS 1` and `DNS 2`.
3. Reboot the Echo Show once so `DataStoreDaemon` drops any pre-cached `JunoAhsAdCard`.

### Layer B — Automated 3-Hour `"Start Photo Frame"` Cloud Routines

Schedule 5 daytime routines (`07:30`, `10:35`, `13:40`, `16:45`, `19:50`) executing the custom command `"start photo frame"` on each Echo Show—either in the Alexa mobile app (*More -> Routines -> +*) or programmatically via `python3 -m echoframe.configure_alexa_ad_free --apply-routines`.

Verify both layers at any time:
```bash
uvx --from git+https://github.com/lifan-builds/echoframe echoframe ad-check --dns <YOUR_DNS_OR_ROUTER_IP>
```

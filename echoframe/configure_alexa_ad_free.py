#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. EchoFrame; Section 7 attribution terms in ATTRIBUTION.md.
"""Manage Echo Show ad-suppression layers (Alexa Cloud Photo Frame routines + NextDNS DDNS).

Layers:
1. Alexa Cloud API (`https://alexa.amazon.com` via `fmu` Chrome extension bridge):
   - `--status`: list active `Ambient Photo Frame · HH:MM` routines across all Echo Shows
   - `--trigger-now`: immediately launch full-screen Photo Frame on all Echo Shows
   - `--apply-routines`: idempotently ensure the 5 daytime 3-hour Photo Frame routines exist
   - `--remove-routines`: cleanly delete managed `Ambient Photo Frame · *` routines
2. Per-Echo NextDNS Linked-IP DDNS (`~/.local/share/alexa-photos/nextdns_ddns.json`, mode 0600):
   - `--refresh-ddns`: hit the saved NextDNS programmatic DDNS endpoint so Linked IP stays
     synced if your residential ISP rotates your public IPv4 address.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from echoframe.sync_amazon_photos import eval_in_chrome_tab, resolve_fmu_bin  # noqa: E402

DEFAULT_DDNS_FILE = Path.home() / ".local" / "share" / "alexa-photos" / "nextdns_ddns.json"
ROUTINE_PREFIX = "Ambient Photo Frame · "
DEFAULT_SCHEDULE_SLOTS: Tuple[Tuple[str, str], ...] = (
    ("073000", "Ambient Photo Frame · 07:30"),
    ("103500", "Ambient Photo Frame · 10:35"),
    ("134000", "Ambient Photo Frame · 13:40"),
    ("164500", "Ambient Photo Frame · 16:45"),
    ("195000", "Ambient Photo Frame · 19:50"),
)
ALEXA_BOOT_URL = "https://alexa.amazon.com/api/devices-v2/device?cached=true"


def build_schedule_trigger_json(customer_id: str, hhmmss: str, tz_id: str = "America/Los_Angeles") -> str:
    inner = {
        "customerId": customer_id,
        "schedule": {
            "@type": "RRULE",
            "triggerTime": hhmmss,
            "triggerTimes": [hhmmss],
            "timeZoneId": tz_id,
            "recurrence": "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=SU,MO,TU,WE,TH,FR,SA",
        },
        "customer": {"@id": customer_id, "@type": "ac:Person"},
    }
    outer = {
        "@type": "com.amazon.alexa.behaviors.model.Trigger",
        "id": None,
        "skillId": "amzn1.ask.1p.routines.schedule",
        "type": "AbsoluteTimeSchedule",
        "payload": json.dumps(inner, separators=(",", ":")),
    }
    return json.dumps(outer, separators=(",", ":"))


def build_photo_frame_sequence_json(validated_payloads: List[Dict[str, Any]]) -> str:
    nodes = [
        {
            "@type": "com.amazon.alexa.behaviors.model.OpaquePayloadOperationNode",
            "type": "Alexa.TextCommand",
            "skillId": "amzn1.ask.1p.tellalexa",
            "operationPayload": payload,
            "context": None,
        }
        for payload in validated_payloads
    ]
    start_node = (
        nodes[0]
        if len(nodes) == 1
        else {"@type": "com.amazon.alexa.behaviors.model.SerialNode", "nodesToExecute": nodes}
    )
    return json.dumps(
        {"@type": "com.amazon.alexa.behaviors.model.Sequence", "startNode": start_node},
        separators=(",", ":"),
    )


def normalize_mac(mac: str) -> str:
    parts = [p.strip().lower().zfill(2) for p in mac.strip().split(":") if p.strip()]
    return ":".join(parts) if len(parts) == 6 else mac.strip().lower()


def is_on_home_lan(gateway_ip: str, gateway_mac: str) -> bool:
    proc = subprocess.run(["arp", "-n", gateway_ip], capture_output=True, text=True, timeout=5, check=False)
    for tok in proc.stdout.split():
        if tok.count(":") == 5 and normalize_mac(tok) == normalize_mac(gateway_mac):
            return True
    return False


def refresh_nextdns_ddns(ddns_file: Path = DEFAULT_DDNS_FILE) -> Dict[str, Any]:
    if not ddns_file.is_file():
        raise FileNotFoundError(f"NextDNS DDNS config not found at {ddns_file}")
    cfg = json.loads(ddns_file.read_text(encoding="utf-8"))
    gw_ip = cfg.get("home_gateway_ip")
    gw_mac = cfg.get("home_gateway_mac")
    if gw_ip and gw_mac and not is_on_home_lan(gw_ip, gw_mac):
        return {
            "profile_id": cfg.get("profile_id"),
            "status": "skipped_away_from_home_lan",
        }
    ddns_url = cfg["ddns_url"]
    req = urllib.request.Request(ddns_url, headers={"User-Agent": "alexa-photos-ddns/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", "replace").strip()
        status = resp.status
    return {
        "profile_id": cfg.get("profile_id"),
        "dns_servers": cfg.get("dns_servers"),
        "http_status": status,
        "linked_ip": body,
    }


def run_alexa_browser_op(mode: str, fmu_bin: Optional[Path] = None) -> Dict[str, Any]:
    fmu = resolve_fmu_bin(fmu_bin)
    slots_js = json.dumps(list(DEFAULT_SCHEDULE_SLOTS))
    prefix_js = json.dumps(ROUTINE_PREFIX)
    mode_js = json.dumps(mode)

    js = f"""(async () => {{
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const csrf = (document.cookie.match(/(?:^|;\\s*)csrf=([^;]+)/)||[])[1] || "";
  const hdrs = {{"Content-Type": "application/json; charset=UTF-8", "csrf": csrf, "Accept": "application/json", "Routines-Version": "3.0.264101"}};
  const devsResp = await fetch("/api/devices-v2/device?cached=true", {{credentials: "include"}}).then(r => r.json());
  const shows = (devsResp.devices || []).filter(d => (d.capabilities || []).includes("TUPLE") && (d.accountName || "").toLowerCase().includes("show"));
  const customerId = (shows[0] && shows[0].deviceOwnerCustomerId) || "";
  const routines = await fetch("/api/behaviors/v2/automations?limit=100", {{credentials: "include"}}).then(r => r.json());
  const managed = (routines || []).filter(r => (r.name || "").startsWith({prefix_js}));
  const op = {mode_js};

  if (op === "status") {{
    return JSON.stringify({{
      shows: shows.map(s => ({{name: s.accountName, serial: s.serialNumber, type: s.deviceType, online: s.online, sw: s.softwareVersion}})),
      managed_routines: managed.map(r => ({{name: r.name, id: r.automationId, status: r.status}}))
    }});
  }}

  if (op === "trigger") {{
    const nodes = shows.map(s => ({{
      "@type": "com.amazon.alexa.behaviors.model.OpaquePayloadOperationNode",
      type: "Alexa.TextCommand",
      skillId: "amzn1.ask.1p.tellalexa",
      operationPayload: {{customerId, deviceType: s.deviceType, deviceSerialNumber: s.serialNumber, text: "start photo frame", locale: "en-US"}}
    }}));
    const res = await fetch("/api/behaviors/preview", {{
      method: "POST", credentials: "include", headers: hdrs,
      body: JSON.stringify({{
        behaviorId: "PREVIEW",
        sequenceJson: JSON.stringify({{"@type": "com.amazon.alexa.behaviors.model.Sequence", startNode: {{"@type": "com.amazon.alexa.behaviors.model.ParallelNode", nodesToExecute: nodes}}}}),
        status: "ENABLED"
      }})
    }});
    return JSON.stringify({{triggered_shows: shows.map(s => s.accountName), http_status: res.status}});
  }}

  if (op === "remove") {{
    const deleted = [];
    for (const r of managed) {{
      await sleep(1200);
      const del = await fetch("/api/behaviors/automations/" + encodeURIComponent(r.automationId), {{method: "DELETE", credentials: "include", headers: hdrs}});
      deleted.push({{name: r.name, id: r.automationId, http_status: del.status}});
    }}
    return JSON.stringify({{deleted}});
  }}

  if (op === "apply") {{
    const validated = [];
    for (const s of shows) {{
      await sleep(1200);
      const vr = await fetch("/api/behaviors/operation/validate", {{
        method: "POST", credentials: "include", headers: hdrs,
        body: JSON.stringify({{
          type: "Alexa.TextCommand",
          operationPayload: JSON.stringify({{deviceType: s.deviceType, deviceSerialNumber: s.serialNumber, locale: "en-US", customerId, text: "start photo frame"}})
        }})
      }}).then(r => r.json());
      validated.push(vr.operationPayload);
    }}
    const seqJson = JSON.stringify({{
      "@type": "com.amazon.alexa.behaviors.model.Sequence",
      startNode: {{"@type": "com.amazon.alexa.behaviors.model.SerialNode", nodesToExecute: validated.map(p => ({{"@type": "com.amazon.alexa.behaviors.model.OpaquePayloadOperationNode", type: "Alexa.TextCommand", skillId: "amzn1.ask.1p.tellalexa", operationPayload: p, context: null}}))}}
    }});
    const applied = [];
    for (const [hhmmss, name] of {slots_js}) {{
      const hit = managed.find(r => r.name === name);
      if (hit) {{
        applied.push({{name, id: hit.automationId, status: "exists"}});
        continue;
      }}
      await sleep(1400);
      const trigStr = JSON.stringify({{
        "@type": "com.amazon.alexa.behaviors.model.Trigger",
        id: null,
        skillId: "amzn1.ask.1p.routines.schedule",
        type: "AbsoluteTimeSchedule",
        payload: JSON.stringify({{
          customerId,
          schedule: {{"@type": "RRULE", triggerTime: hhmmss, triggerTimes: [hhmmss], timeZoneId: "America/Los_Angeles", recurrence: "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=SU,MO,TU,WE,TH,FR,SA"}},
          customer: {{"@id": customerId, "@type": "ac:Person"}}
        }})
      }});
      const cr = await fetch("/api/behaviors/automations", {{
        method: "POST", credentials: "include", headers: hdrs,
        body: JSON.stringify({{name, status: "ENABLED", triggerJson: trigStr, triggerJsonList: [trigStr], sequenceJson: seqJson}})
      }});
      const cj = await cr.json();
      applied.push({{name, id: cj.automationId, http_status: cr.status}});
    }}
    return JSON.stringify({{applied}});
  }}
  throw new Error("Unknown op: " + op);
}})()"""

    raw = eval_in_chrome_tab(fmu, ALEXA_BOOT_URL, js, timeout=60)
    json_line = [ln for ln in raw.splitlines() if ln.startswith("{")][-1]
    return json.loads(json_line)


JUNO_AD_HOSTS: Tuple[str, ...] = (
    "trck.ahs.prod-na.turntable.sonic.advertising.amazon.dev",
    "prod.na.jet.amazon",
    "aax-us-east.amazon-adsystem.com",
    "unagi-na.amazon.com",
)
ALEXA_ALLOW_HOSTS: Tuple[str, ...] = (
    "m.media-amazon.com",
    "avs-alexa-14-na.amazon.com",
)
SINKHOLE_ADDRS = {"0.0.0.0", "127.0.0.1", "::", "::1"}


def classify_dig_answer(stdout: str) -> Tuple[str, List[str]]:
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if any(ln.startswith(";") for ln in lines):
        return "error", lines
    addrs = [ln for ln in lines if not ln.endswith(".")]
    if all(a in SINKHOLE_ADDRS for a in addrs):
        return "blocked", lines
    return "resolves", lines


def verify_dns_sinkhole(dns_ip: Optional[str] = None) -> Dict[str, Any]:
    server = [f"@{dns_ip}"] if dns_ip else []
    probes: List[Dict[str, Any]] = []
    for role, hosts, want in (("juno_ad", JUNO_AD_HOSTS, "blocked"), ("alexa_allow", ALEXA_ALLOW_HOSTS, "resolves")):
        for host in hosts:
            proc = subprocess.run(
                ["dig", "+short", "+time=3", "+tries=1", *server, host],
                capture_output=True,
                text=True,
                timeout=6,
                check=False,
            )
            status, answers = classify_dig_answer(proc.stdout)
            probes.append({"host": host, "role": role, "status": status, "answers": answers, "ok": status == want})
    return {"dns_server": dns_ip or "system", "ok": all(p["ok"] for p in probes), "probes": probes}


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--status", action="store_true", help="Inspect Echo Show status & scheduled Photo Frame routines")
    p.add_argument("--trigger-now", action="store_true", help="Immediately start Photo Frame on all Echo Shows")
    p.add_argument("--apply-routines", action="store_true", help="Ensure all 5 daytime Photo Frame routines exist")
    p.add_argument("--remove-routines", action="store_true", help="Delete managed Ambient Photo Frame routines")
    p.add_argument("--refresh-ddns", action="store_true", help="Refresh NextDNS Linked-IP DDNS endpoint")
    p.add_argument("--verify-dns", metavar="DNS_IP", help="Verify ad-domain sinkholing & Alexa allowlist on DNS_IP")
    p.add_argument("--ddns-file", type=Path, default=DEFAULT_DDNS_FILE)
    args = p.parse_args(argv)

    report: Dict[str, Any] = {}
    if args.refresh_ddns:
        report["ddns"] = refresh_nextdns_ddns(args.ddns_file)
    if args.verify_dns:
        report["dns_probe"] = verify_dns_sinkhole(args.verify_dns)
    if args.remove_routines:
        report["removed"] = run_alexa_browser_op("remove")
    if args.apply_routines:
        report["applied"] = run_alexa_browser_op("apply")
    if args.trigger_now:
        report["triggered"] = run_alexa_browser_op("trigger")
    if args.status or not report:
        report["alexa"] = run_alexa_browser_op("status")

    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

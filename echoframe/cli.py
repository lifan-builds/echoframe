# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. EchoFrame; Section 7 attribution terms in ATTRIBUTION.md.
"""EchoFrame command line: `echoframe ad-check | curate | export`.

`data/` and `private/` are read from and written to the current directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import version
from typing import List, Optional

ATTRIBUTION = "Based on EchoFrame by lifan-builds (https://github.com/lifan-builds/echoframe)"


def ad_check(argv: List[str]) -> int:
    from echoframe.configure_alexa_ad_free import verify_dns_sinkhole

    p = argparse.ArgumentParser(
        prog="echoframe ad-check",
        description="Verify the 4 Echo Show Juno ad-fill beacons are sinkholed while Alexa voice and photo hosts still resolve (no API key, no Amazon login).",
    )
    p.add_argument("--dns", metavar="DNS_IP", help="DNS server to probe, e.g. your router, Pi-hole, or NextDNS IP (default: system resolver)")
    p.add_argument("--json", action="store_true", help="Print the full JSON report")
    args = p.parse_args(argv)

    report = verify_dns_sinkhole(args.dns)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"DNS server: {report['dns_server']}")
        for probe in report["probes"]:
            mark = "PASS" if probe["ok"] else "FAIL"
            print(f"  {mark}  {probe['role']:<11}  {probe['status']:<8}  {probe['host']}  {' '.join(probe['answers'])}")
        print("Echo Show ads are sinkholed." if report["ok"] else "Not ad-free yet: see docs/echo-show-ad-architecture.md")
    return 0 if report["ok"] else 1


def curate(argv: List[str]) -> int:
    from echoframe.extract_iphone_album import main

    main(argv, prog="echoframe curate")
    return 0


def export(argv: List[str]) -> int:
    from echoframe.export_amazon_photos import main

    main(argv, prog="echoframe export")
    return 0


COMMANDS = {
    "ad-check": (ad_check, "Verify Echo Show Juno ad-beacon DNS sinkholing (zero API keys)"),
    "curate": (curate, "AI-curate a photo folder or USB iPhone into 16:10 slides (needs OPENAI_API_KEY)"),
    "export": (export, "Export curated slides as an Amazon Photos upload bundle for Echo Show"),
}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="echoframe",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {name:<9} {help_}" for name, (_, help_) in COMMANDS.items()),
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"echoframe {version('echoframe')}\n{ATTRIBUTION}\nLicense: AGPL-3.0-only with Section 7 attribution terms (ATTRIBUTION.md)",
    )
    p.add_argument("command", choices=COMMANDS, metavar="command", help="one of: " + ", ".join(COMMANDS))
    p.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    return COMMANDS[args.command][0](args.args)


if __name__ == "__main__":
    sys.exit(main())

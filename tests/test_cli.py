# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. EchoFrame; Section 7 attribution terms in ATTRIBUTION.md.
import io
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock

from echoframe import cli
from echoframe.configure_alexa_ad_free import JUNO_AD_HOSTS, classify_dig_answer, verify_dns_sinkhole


def fake_dig(answers):
    def run(cmd, **_):
        return subprocess.CompletedProcess(cmd, 0, stdout=answers[cmd[-1]], stderr="")

    return run


class VersionTests(unittest.TestCase):
    def test_version_prints_agpl_attribution(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as exc:
            cli.main(["--version"])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("Based on EchoFrame by lifan-builds (https://github.com/lifan-builds/echoframe)", out.getvalue())


class AdCheckTests(unittest.TestCase):
    def test_classify_dig_answer(self):
        self.assertEqual(classify_dig_answer("0.0.0.0\n")[0], "blocked")
        self.assertEqual(classify_dig_answer("")[0], "blocked")
        self.assertEqual(classify_dig_answer("c.media-amazon.com.\n99.86.100.203\n")[0], "resolves")
        self.assertEqual(classify_dig_answer(";; connection timed out; no servers could be reached\n")[0], "error")

    def test_sinkholed_network_passes_and_leaky_network_fails(self):
        allow = {"m.media-amazon.com": "99.86.100.203\n", "avs-alexa-14-na.amazon.com": "52.94.1.1\n"}
        blocked = {**allow, **{h: "0.0.0.0\n" for h in JUNO_AD_HOSTS}}
        with mock.patch("echoframe.configure_alexa_ad_free.subprocess.run", side_effect=fake_dig(blocked)) as run:
            report = verify_dns_sinkhole("192.168.1.1")
        self.assertTrue(report["ok"])
        self.assertIn("@192.168.1.1", run.call_args.args[0])

        leaky = {**blocked, "prod.na.jet.amazon": "3.3.3.3\n", "m.media-amazon.com": "0.0.0.0\n"}
        with mock.patch("echoframe.configure_alexa_ad_free.subprocess.run", side_effect=fake_dig(leaky)) as run:
            report = verify_dns_sinkhole()
        self.assertFalse(report["ok"])
        self.assertEqual({p["host"] for p in report["probes"] if not p["ok"]}, {"prod.na.jet.amazon", "m.media-amazon.com"})
        self.assertFalse(any(arg.startswith("@") for arg in run.call_args.args[0]))

    def test_cli_ad_check_exit_code(self):
        report = {"dns_server": "system", "ok": False, "probes": [{"host": "prod.na.jet.amazon", "role": "juno_ad", "status": "resolves", "answers": ["3.3.3.3"], "ok": False}]}
        with mock.patch("echoframe.configure_alexa_ad_free.verify_dns_sinkhole", return_value=report), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["ad-check"]), 1)
        self.assertIn("FAIL", out.getvalue())

    def test_cli_forwards_subcommand_args(self):
        with mock.patch("echoframe.export_amazon_photos.main") as export_main:
            self.assertEqual(cli.main(["export", "--album-dir", "x", "--mark-synced"]), 0)
        export_main.assert_called_once_with(["--album-dir", "x", "--mark-synced"], prog="echoframe export")
        with mock.patch("echoframe.extract_iphone_album.main") as curate_main:
            cli.main(["curate", "--source-dir", "pics"])
        curate_main.assert_called_once_with(["--source-dir", "pics"], prog="echoframe curate")


if __name__ == "__main__":
    unittest.main()

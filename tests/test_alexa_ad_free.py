import json
import unittest

from echoframe.configure_alexa_ad_free import (
    DEFAULT_SCHEDULE_SLOTS,
    build_photo_frame_sequence_json,
    build_schedule_trigger_json,
    normalize_mac,
)


class AlexaAdFreeTests(unittest.TestCase):
    def test_schedule_trigger_double_encodes_payload_for_alexa_automations_api(self):
        raw = build_schedule_trigger_json("CUST123", "164500", "America/Los_Angeles")
        outer = json.loads(raw)
        self.assertEqual(outer["type"], "AbsoluteTimeSchedule")
        self.assertEqual(outer["skillId"], "amzn1.ask.1p.routines.schedule")
        self.assertIsInstance(outer["payload"], str)
        inner = json.loads(outer["payload"])
        self.assertEqual(inner["schedule"]["triggerTime"], "164500")
        self.assertEqual(inner["customer"]["@id"], "CUST123")

    def test_photo_frame_sequence_serializes_multi_device_nodes(self):
        seq = json.loads(
            build_photo_frame_sequence_json(
                [
                    {"deviceSerialNumber": "DSN1", "text": "start photo frame"},
                    {"deviceSerialNumber": "DSN2", "text": "start photo frame"},
                ]
            )
        )
        self.assertEqual(seq["startNode"]["@type"], "com.amazon.alexa.behaviors.model.SerialNode")
        self.assertEqual(len(seq["startNode"]["nodesToExecute"]), 2)
        self.assertEqual(len(DEFAULT_SCHEDULE_SLOTS), 5)

    def test_normalize_mac_handles_bsd_arp_short_octets(self):
        self.assertEqual(normalize_mac("00:53:00:ab:1:b"), "00:53:00:ab:01:0b")
        self.assertEqual(normalize_mac("00:53:00:AB:1D:1B"), "00:53:00:ab:1d:1b")


if __name__ == "__main__":
    unittest.main()

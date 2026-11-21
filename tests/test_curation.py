import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from scripts.extract_iphone_album import normalize_vision_result, scan_photo_folder, select_curated_winners


def make_item(lid, date, dhash, tags=()):
    return {"local_id": lid, "creation_date": date, "dhash_hex": dhash, "scene_tags": list(tags), "aesthetic_score": 7.0}


def vision(theme="snow_mountain", score=8.0, channels=("travel_outdoors",)):
    return {"ambient_safe": True, "frame_score": score, "channels": list(channels), "visual_theme": theme, "display_caption": "Quiet ridge at dawn"}


class CurationTests(unittest.TestCase):
    def test_pets_channel_derived_from_tags_and_theme(self):
        by_tag = normalize_vision_result(make_item("a", "2025-01-01T10:00:00Z", "0" * 16, ["dog", "backyard"]), vision())
        by_theme = normalize_vision_result(make_item("b", "2025-01-01T10:00:00Z", "0" * 16), vision(theme="dog_portrait"))
        plain = normalize_vision_result(make_item("c", "2025-01-01T10:00:00Z", "0" * 16, ["hotdog stand"]), vision())
        self.assertIn("pets", by_tag["channels"])
        self.assertIn("pets", by_theme["channels"])
        self.assertNotIn("pets", plain["channels"])

    def test_uncapped_selection_keeps_every_diverse_winner_above_quality_bar(self):
        items, vmap = [], {}
        for i in range(30):
            it = make_item(f"id{i}", f"2025-{(i % 12) + 1:02d}-{(i // 12) + 1:02d}T10:00:00Z", hashlib.sha256(str(i).encode()).hexdigest()[:16])
            items.append(it)
            vmap[it["local_id"]] = normalize_vision_result(it, vision(score=6.0 if i % 3 == 0 else 8.2))
        winners = select_curated_winners(items, vmap, Path("/nonexistent"))
        self.assertEqual(len(winners), 20)
        self.assertTrue(all(w["_vision"]["frame_score"] >= 7.2 for w in winners))
        self.assertEqual(len(select_curated_winners(items, vmap, Path("/nonexistent"), target_count=5)), 5)

    def test_scan_photo_folder_ingests_standalone_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            thumbs = Path(tmp) / "thumbs"
            src.mkdir()
            Image.new("RGB", (1200, 800), (40, 90, 140)).save(src / "IMG_0001.jpg", format="JPEG")
            items = scan_photo_folder(src, thumbs)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["local_id"], "IMG_0001.jpg")
            self.assertEqual(len(items[0]["dhash_hex"]), 16)
            self.assertTrue(Path(items[0]["thumb_path"]).is_file())


if __name__ == "__main__":
    unittest.main()

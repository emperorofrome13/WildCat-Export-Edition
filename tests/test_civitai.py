import unittest

from app.civitai import build_generation_meta, match_civitai_items


def asset(asset_id, seed, width=832, height=1216, posted=False):
    return {"id": asset_id, "seed": seed, "width": width, "height": height, "civitai_posted": posted}


def item(image_id, post_id, seed, width=832, height=1216):
    return {
        "id": image_id,
        "postId": post_id,
        "width": width,
        "height": height,
        "url": f"https://image.civitai.com/{image_id}.jpeg",
        "meta": {"seed": seed, "prompt": "x"},
    }


class MatchCivitaiItemsTests(unittest.TestCase):
    def test_matches_by_seed_and_dimensions(self):
        matches = match_civitai_items([asset("a1", 1938345220)], [item(9173928, 1981754, 1938345220)])
        self.assertIn("a1", matches)
        self.assertEqual(matches["a1"]["post_id"], 1981754)
        self.assertEqual(matches["a1"]["url"], "https://civitai.red/posts/1981754")

    def test_skips_items_without_seed_or_post(self):
        matches = match_civitai_items(
            [asset("a1", 1)],
            [{"id": 1, "postId": 2, "width": 832, "height": 1216, "meta": {}},
             {"id": 2, "postId": None, "width": 832, "height": 1216, "meta": {"seed": 1}},
             {"id": 3, "postId": 3, "width": 832, "height": 1216, "meta": {"seed": "not-a-number"}}],
        )
        self.assertEqual(matches, {})

    def test_dimension_mismatch_does_not_match(self):
        matches = match_civitai_items([asset("a1", 5, 832, 1216)], [item(1, 2, 5, 1024, 1024)])
        self.assertEqual(matches, {})

    def test_already_posted_assets_are_never_rematched(self):
        matches = match_civitai_items([asset("a1", 5, posted=True)], [item(1, 2, 5)])
        self.assertEqual(matches, {})

    def test_first_match_wins_for_duplicate_assets(self):
        matches = match_civitai_items([asset("a1", 5)], [item(1, 2, 5), item(3, 4, 5)])
        self.assertEqual(matches["a1"]["post_id"], 2)

    def test_null_seed_assets_are_skipped(self):
        matches = match_civitai_items([asset("a1", None)], [item(1, 2, 5)])
        self.assertEqual(matches, {})


class GenerationMetaTests(unittest.TestCase):
    def test_builds_meta_with_tool_and_fields(self):
        meta = build_generation_meta("a prompt", "a negative", 42)
        self.assertEqual(meta["tool"], "WildCat Harness")
        self.assertEqual(meta["prompt"], "a prompt")
        self.assertEqual(meta["negativePrompt"], "a negative")
        self.assertEqual(meta["seed"], 42)

    def test_omits_empty_fields_and_merges_extra(self):
        meta = build_generation_meta("", "", None, {"steps": 30, "sampler": "", "cfgScale": 5})
        self.assertNotIn("prompt", meta)
        self.assertNotIn("negativePrompt", meta)
        self.assertNotIn("seed", meta)
        self.assertEqual(meta["steps"], 30)
        self.assertNotIn("sampler", meta)
        self.assertEqual(meta["cfgScale"], 5)


if __name__ == "__main__":
    unittest.main()

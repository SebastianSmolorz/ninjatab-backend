"""orig + UVDoc + flat: the processed images, and the fallbacks to the original."""

import math
from unittest import mock

import cv2
import numpy as np
from django.test import SimpleTestCase

from ninjatab.tabs.receipt_scanning import deskew
from ninjatab.tabs.receipt_scanning.base import ScanContext, ScanResult
from ninjatab.tabs.receipt_scanning.sources import mix_refs
from ninjatab.tabs.receipt_scanning.strategies import resolve_strategy


def _ctx(image_bytes: bytes) -> ScanContext:
    return ScanContext(image_bytes=image_bytes, content_type="image/jpeg",
                       default_currency="USD", tab_id="t")


def _jpeg(image: np.ndarray) -> bytes:
    return cv2.imencode(".jpg", image)[1].tobytes()


class MixRefsTests(SimpleTestCase):
    def test_bent_receipt_gets_both_processed_images(self):
        page = deskew._synthetic_receipt(0.0)
        h, w = page.shape[:2]
        xx, yy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
        bend = 0.002 * (xx - 135) ** 2 + math.tan(math.radians(5)) * (xx - 135)
        warped = cv2.remap(page, xx, yy - bend, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        refs, metrics = mix_refs(_ctx(_jpeg(warped)))
        self.assertEqual(metrics["preprocess_arms"], ["original", "uvdoc", "flat"])
        self.assertGreater(metrics["mix_uvdoc_ms"], 0)
        self.assertIsNotNone(metrics["mix_flat_ms"])
        self.assertEqual(metrics["mix_wait_ms"], 0)  # nothing else holds the lock
        self.assertTrue(all(r.startswith("data:image/jpeg;base64,") for r in refs))
        self.assertEqual(len(set(refs)), 3)

    def test_undecodable_image_sends_the_original_three_times(self):
        refs, metrics = mix_refs(_ctx(b"not an image"))
        self.assertEqual(metrics["preprocess_arms"], ["original"] * 3)
        self.assertIsNone(metrics["mix_uvdoc_ms"])
        self.assertEqual(len(set(refs)), 1)

    def test_unwarp_error_falls_back_to_the_original(self):
        image = _jpeg(deskew._synthetic_receipt(0.0))
        with mock.patch("ninjatab.tabs.receipt_scanning.unwarp.unwarp_image", side_effect=RuntimeError):
            refs, metrics = mix_refs(_ctx(image))
        self.assertEqual(metrics["preprocess_arms"][:2], ["original", "original"])
        self.assertEqual(refs[1], refs[0])


class MixCallsTests(SimpleTestCase):
    def test_failed_processed_call_drops_out_original_failure_raises(self):
        strategy = resolve_strategy("verified_consensus_mix")
        ctx = _ctx(b"x")
        ctx.preprocess_metrics = {"mix_failed_calls": []}
        ok = {"annotation": {"items": []}, "parse_error": False, "ocr_markdown": "",
              "ocr_pages": 1, "ocr_markdown_chars": 0, "call_ms": 1}

        def fake(client, url, *a, **k):
            if url == "uvdoc":
                raise TimeoutError
            return ok

        path = "ninjatab.tabs.receipt_scanning.strategies"
        with mock.patch(f"{path}.run_single_ocr", side_effect=fake), \
                mock.patch(f"{path}.mistral_client"), mock.patch(f"{path}.sentry_sdk"):
            results = strategy.call_mistral(["original", "uvdoc", "flat"], ctx)
            self.assertEqual([r["annotation"] is None for r in results], [False, True, False])
            self.assertEqual(ctx.preprocess_metrics["mix_failed_calls"], ["uvdoc"])
            with self.assertRaises(TimeoutError):
                strategy.call_mistral(["uvdoc", "flat", "flat"], ctx)

    def test_metrics_name_the_chosen_arm_and_time_each_call(self):
        strategy = resolve_strategy("verified_consensus_mix")
        ctx = _ctx(b"x")
        reading = {"annotation": None, "parse_error": False, "ocr_markdown": "",
                   "ocr_pages": 0, "ocr_markdown_chars": 0}
        path = "ninjatab.tabs.receipt_scanning.strategies"
        refs = ["o", "u", "f"]
        mixed = {"preprocess_arms": ["original", "uvdoc", "original"], "mix_uvdoc_ms": 5, "mix_flat_ms": 7}
        with mock.patch(f"{path}.mix_refs", return_value=(refs, dict(mixed))), \
                mock.patch(f"{path}.mistral_client"), \
                mock.patch(f"{path}.run_single_ocr",
                           side_effect=lambda c, url, *a, **k: {**reading, "call_ms": {"o": 10, "u": 20, "f": 30}[url]}), \
                mock.patch(f"{path}.VerifiedConsensusStrategy.post_process", lambda self, ocr, ctx: ScanResult(
                    document_annotation={}, date="", metrics={**self.base_metrics(ctx), "consensus_selected_index": 1})):
            metrics = strategy.run(ctx).metrics
        self.assertEqual(metrics["mix_chosen_arm"], "uvdoc")
        self.assertEqual((metrics["mix_original_call_ms"], metrics["mix_uvdoc_call_ms"],
                          metrics["mix_flat_call_ms"]), (10, 20, 30))
        self.assertEqual((metrics["mix_uvdoc_ms"], metrics["mix_flat_ms"]), (5, 7))
        self.assertEqual(metrics["preprocess_arms"], ["original", "uvdoc", "original"])
        self.assertEqual(metrics["mix_failed_calls"], [])
        for key in ("pre_ms", "mistral_call_ms", "post_ms", "scan_total_ms"):
            self.assertIsInstance(metrics[key], int, key)

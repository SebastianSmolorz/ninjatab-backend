"""An unresolvable strategy name must be reported, not silently downgraded."""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from ninjatab.tabs.receipt_scanning.strategies import resolve_strategy


@override_settings(RECEIPT_SCAN_STRATEGY="baseline_mistral_ocr")
class ResolveStrategyFallbackTests(SimpleTestCase):
    def _resolve(self, value):
        with mock.patch("ninjatab.tabs.receipt_scanning.strategies.sentry_sdk") as sentry:
            strategy = resolve_strategy(value)
        return strategy, sentry.capture_message

    def test_unknown_option_value_warns(self):
        strategy, capture = self._resolve(SimpleNamespace(active=True, value="verified_consensus_split"))
        self.assertEqual(strategy.name, "baseline_mistral_ocr")
        capture.assert_called_once()
        self.assertIn("verified_consensus_split", capture.call_args.args[0])
        self.assertEqual(capture.call_args.kwargs["level"], "warning")

    def test_known_option_value_is_silent(self):
        strategy, capture = self._resolve(SimpleNamespace(active=True, value="verified_consensus"))
        self.assertEqual(strategy.name, "verified_consensus")
        capture.assert_not_called()

    def test_inactive_option_falls_back_silently(self):
        strategy, capture = self._resolve(SimpleNamespace(active=False, value="nonsense"))
        self.assertEqual(strategy.name, "baseline_mistral_ocr")
        capture.assert_not_called()

    @override_settings(RECEIPT_SCAN_STRATEGY="gone")
    def test_unknown_setting_warns(self):
        strategy, capture = self._resolve(None)
        self.assertEqual(strategy.name, "baseline_mistral_ocr")
        capture.assert_called_once()

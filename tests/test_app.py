"""Unit tests for QuickVRCScaler.

Covers the pure UI-state logic: warning generation and incoming-OSC event
handling. Network start-up (OSC server, OSCQuery) is patched out so tests
do not bind sockets or touch zeroconf.
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Make the app module importable when run from repo root or tests/.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import quickvrcscaler as qvc  # noqa: E402


def _make_app() -> tuple[tk.Tk, qvc.App]:
    root = tk.Tk()
    root.withdraw()
    app = qvc.App(root)
    app.client = MagicMock()
    return root, app


class _BaseAppTest(unittest.TestCase):
    """Patches network start-up so tests stay hermetic."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._orig_start_server = qvc.App._start_server
        cls._orig_start_query = qvc.App._start_oscquery
        cls._orig_poll_query = qvc.App._poll_oscquery
        cls._orig_udp_client = qvc.udp_client.SimpleUDPClient
        qvc.App._start_server = lambda self: None  # type: ignore[assignment]
        qvc.App._start_oscquery = lambda self: None  # type: ignore[assignment]
        qvc.App._poll_oscquery = lambda self: None  # type: ignore[assignment]
        qvc.udp_client.SimpleUDPClient = lambda *a, **kw: MagicMock()  # type: ignore[assignment]

    @classmethod
    def tearDownClass(cls) -> None:
        qvc.App._start_server = cls._orig_start_server  # type: ignore[assignment]
        qvc.App._start_oscquery = cls._orig_start_query  # type: ignore[assignment]
        qvc.App._poll_oscquery = cls._orig_poll_query  # type: ignore[assignment]
        qvc.udp_client.SimpleUDPClient = cls._orig_udp_client  # type: ignore[assignment]

    def setUp(self) -> None:
        self.root, self.app = _make_app()

    def tearDown(self) -> None:
        self.root.destroy()


class WarningTests(_BaseAppTest):
    def test_no_state_no_warning(self) -> None:
        self.app._refresh_warning()
        self.assertEqual(self.app.warning_var.get(), "")

    def test_safe_height_no_warning(self) -> None:
        self.app._refresh_warning(pending=1.6)
        self.assertEqual(self.app.warning_var.get(), "")

    def test_above_safe_range_warns(self) -> None:
        self.app._refresh_warning(pending=150.0)
        self.assertIn("outside", self.app.warning_var.get().lower())

    def test_below_safe_range_warns(self) -> None:
        self.app._refresh_warning(pending=0.05)
        self.assertIn("outside", self.app.warning_var.get().lower())

    def test_scaling_blocked_warns(self) -> None:
        self.app.cur_allowed = False
        self.app._refresh_warning(pending=1.6)
        self.assertIn("disabled", self.app.warning_var.get().lower())

    def test_scaling_allowed_silent(self) -> None:
        self.app.cur_allowed = True
        self.app._refresh_warning(pending=1.6)
        self.assertEqual(self.app.warning_var.get(), "")

    def test_below_udon_min_warns(self) -> None:
        self.app.cur_min = 0.5
        self.app._refresh_warning(pending=0.3)
        self.assertIn("minimum", self.app.warning_var.get().lower())

    def test_above_udon_max_warns(self) -> None:
        self.app.cur_max = 3.0
        self.app._refresh_warning(pending=4.0)
        self.assertIn("maximum", self.app.warning_var.get().lower())

    def test_within_udon_range_silent(self) -> None:
        self.app.cur_min = 0.5
        self.app.cur_max = 3.0
        self.app._refresh_warning(pending=1.6)
        self.assertEqual(self.app.warning_var.get(), "")


class EventApplyTests(_BaseAppTest):
    def test_height_updates_state_and_label(self) -> None:
        self.app._apply_event("height", 1.75)
        self.assertAlmostEqual(self.app.cur_height or 0.0, 1.75, places=5)
        self.assertIn("1.750", self.app.info_height_var.get())

    def test_height_event_does_not_echo_to_osc(self) -> None:
        self.app._apply_event("height", 1.75)
        self.app.client.send_message.assert_not_called()

    def test_height_event_clamps_slider_into_range(self) -> None:
        self.app._apply_event("height", 50.0)
        # Slider clamps to [SLIDER_MIN, SLIDER_MAX] but readout shows true.
        self.assertLessEqual(self.app.slider_var.get(), qvc.SLIDER_MAX)
        self.assertIn("50.000", self.app.info_height_var.get())

    def test_min_event(self) -> None:
        self.app._apply_event("min", 0.5)
        self.assertAlmostEqual(self.app.cur_min or 0.0, 0.5, places=5)
        self.assertIn("0.500", self.app.info_min_var.get())

    def test_max_event(self) -> None:
        self.app._apply_event("max", 3.0)
        self.assertAlmostEqual(self.app.cur_max or 0.0, 3.0, places=5)
        self.assertIn("3.000", self.app.info_max_var.get())

    def test_allowed_event_false(self) -> None:
        self.app._apply_event("allowed", False)
        self.assertIs(self.app.cur_allowed, False)
        self.assertIn("BLOCKED", self.app.info_allowed_var.get())

    def test_allowed_event_true(self) -> None:
        self.app._apply_event("allowed", True)
        self.assertIs(self.app.cur_allowed, True)
        self.assertIn("allowed", self.app.info_allowed_var.get().lower())

    def test_garbage_height_value_is_ignored(self) -> None:
        self.app._apply_event("height", "not-a-number")
        self.assertIsNone(self.app.cur_height)


class SendTests(_BaseAppTest):
    def test_send_height_sends_float(self) -> None:
        self.app._send_height(2.0)
        self.app.client.send_message.assert_called_once()
        args, _ = self.app.client.send_message.call_args
        self.assertEqual(args[0], qvc.ADDR_HEIGHT)
        self.assertIsInstance(args[1], float)
        self.assertAlmostEqual(args[1], 2.0)

    def test_reset_sends_default(self) -> None:
        self.app._reset()
        args, _ = self.app.client.send_message.call_args
        self.assertEqual(args[0], qvc.ADDR_HEIGHT)
        self.assertAlmostEqual(args[1], 1.6)

    def test_entry_clamps_to_absolute_bounds(self) -> None:
        self.app.entry_var.set("99999")
        self.app._on_entry_submit()
        args, _ = self.app.client.send_message.call_args
        self.assertLessEqual(args[1], 10000.0)

    def test_entry_rejects_non_numeric(self) -> None:
        self.app.entry_var.set("tall")
        self.app._on_entry_submit()
        self.app.client.send_message.assert_not_called()
        self.assertIn("not a number", self.app.warning_var.get())


class QuickButtonTests(_BaseAppTest):
    def test_apply_preset_sends_exact_value(self) -> None:
        self.app._apply_preset(2.0)
        args, _ = self.app.client.send_message.call_args
        self.assertEqual(args[0], qvc.ADDR_HEIGHT)
        self.assertAlmostEqual(args[1], 2.0)

    def test_apply_preset_clamps_high(self) -> None:
        self.app._apply_preset(99999.0)
        args, _ = self.app.client.send_message.call_args
        self.assertLessEqual(args[1], 10000.0)

    def test_apply_preset_clamps_low(self) -> None:
        self.app._apply_preset(0.0)
        args, _ = self.app.client.send_message.call_args
        self.assertGreaterEqual(args[1], 0.01)

    def test_apply_preset_above_slider_range_still_sent(self) -> None:
        self.app._apply_preset(25.0)
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], 25.0)
        # Slider gets clamped but the actual value is sent unchanged.
        self.assertLessEqual(self.app.slider_var.get(), qvc.SLIDER_MAX)

    def test_apply_scale_uses_reported_height(self) -> None:
        self.app.cur_height = 1.6
        self.app._apply_scale(2.0)
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], 3.2)

    def test_apply_scale_falls_back_to_slider(self) -> None:
        self.app.cur_height = None
        self.app.slider_var.set(2.0)
        self.app._apply_scale(0.5)
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], 1.0)

    def test_apply_scale_minus_50_percent(self) -> None:
        self.app.cur_height = 4.0
        self.app._apply_scale(0.5)
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], 2.0)

    def test_apply_scale_plus_25_percent(self) -> None:
        self.app.cur_height = 1.6
        self.app._apply_scale(1.25)
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], 2.0)


class _FakeHostInfo:
    def __init__(self, name: str) -> None:
        self.name = name


class OSCQueryServicePickTests(unittest.TestCase):
    """Selection logic when multiple OSCQuery apps (e.g. VRCFT) are present."""

    def test_returns_none_for_empty(self) -> None:
        self.assertIsNone(qvc.App._pick_vrchat_service([]))

    def test_single_candidate_returned(self) -> None:
        svc = object()
        result = qvc.App._pick_vrchat_service([(svc, _FakeHostInfo("Anything"), None)])
        self.assertIs(result, svc)

    def test_prefers_vrchat_named_service(self) -> None:
        vrcft = object()
        vrchat = object()
        candidates = [
            (vrcft, _FakeHostInfo("VRCFT"), None),
            (vrchat, _FakeHostInfo("VRChat-Client-ABCDEF"), None),
        ]
        self.assertIs(qvc.App._pick_vrchat_service(candidates), vrchat)

    def test_vrchat_match_is_case_insensitive(self) -> None:
        vrcft = object()
        vrchat = object()
        candidates = [
            (vrcft, _FakeHostInfo("VRCFT"), None),
            (vrchat, _FakeHostInfo("vrchat-client"), None),
        ]
        self.assertIs(qvc.App._pick_vrchat_service(candidates), vrchat)

    def test_falls_back_to_first_when_no_vrchat_match(self) -> None:
        first = object()
        second = object()
        candidates = [
            (first, _FakeHostInfo("VRCFT"), None),
            (second, _FakeHostInfo("OtherApp"), None),
        ]
        self.assertIs(qvc.App._pick_vrchat_service(candidates), first)

    def test_handles_missing_host_info_name(self) -> None:
        broken = object()
        vrchat = object()
        candidates = [
            (broken, _FakeHostInfo(None), None),  # type: ignore[arg-type]
            (vrchat, _FakeHostInfo("VRChat"), None),
        ]
        self.assertIs(qvc.App._pick_vrchat_service(candidates), vrchat)


@unittest.skipUnless(qvc._OSCQUERY_AVAILABLE, "tinyoscquery not available")
class OSCQueryPollLifecycleTests(_BaseAppTest):
    """Regression tests for the 2-hour CPU/freeze bug.

    The original code created a fresh ``OSCQueryBrowser`` (and thus a fresh
    ``Zeroconf`` instance, never closed) on every 10-second poll, and used
    ``requests.get`` calls with no timeout. After ~2 hours, hundreds of
    leaked zeroconf threads + any stalled HTTP request would starve the GIL
    and freeze the Tk UI. These tests pin the new behaviour in place.
    """

    def test_browser_is_created_only_once(self) -> None:
        """Many poll cycles must reuse the same OSCQueryBrowser instance."""
        with patch.object(qvc, "OSCQueryBrowser") as mock_browser_cls, \
             patch.object(qvc.time, "sleep"):
            mock_browser_cls.return_value.get_discovered_oscquery.return_value = []
            for _ in range(10):
                self.app._poll_oscquery_worker()
            self.assertEqual(mock_browser_cls.call_count, 1)
            # And the app should be holding on to that one instance.
            self.assertIs(self.app._browser, mock_browser_cls.return_value)

    def test_poll_skips_when_worker_in_flight(self) -> None:
        """If a worker hasn't finished, _poll_oscquery must not spawn another."""
        # _BaseAppTest patches _poll_oscquery out for hermetic startup; reach
        # through to the original to actually exercise the guard.
        real_poll = type(self)._orig_poll_query
        with patch.object(qvc, "_OSCQUERY_AVAILABLE", True), \
             patch.object(qvc.threading, "Thread") as mock_thread, \
             patch.object(self.app.root, "after"):
            self.app._poll_in_flight = True
            real_poll(self.app)
            mock_thread.assert_not_called()

    def test_poll_spawns_when_idle(self) -> None:
        """When no worker is in flight, _poll_oscquery should kick one off."""
        real_poll = type(self)._orig_poll_query
        with patch.object(qvc, "_OSCQUERY_AVAILABLE", True), \
             patch.object(qvc.threading, "Thread") as mock_thread, \
             patch.object(self.app.root, "after"):
            self.app._poll_in_flight = False
            real_poll(self.app)
            mock_thread.assert_called_once()
            # Marking the flag must happen at spawn time, not inside the worker —
            # otherwise overlapping polls before the thread starts could double up.
            self.assertTrue(self.app._poll_in_flight)

    def test_worker_clears_flag_even_on_exception(self) -> None:
        """A blow-up inside the worker must not leave polling permanently disabled."""
        with patch.object(self.app, "_poll_oscquery_once",
                          side_effect=RuntimeError("boom")):
            self.app._poll_in_flight = True
            with self.assertRaises(RuntimeError):
                self.app._poll_oscquery_worker()
            self.assertFalse(self.app._poll_in_flight)

    def test_http_get_json_always_passes_timeout(self) -> None:
        """The HTTP helper must pass a positive timeout — that's the whole point."""
        with patch.object(qvc, "requests") as mock_requests:
            mock_requests.RequestException = Exception
            mock_requests.get.side_effect = Exception("simulated")
            result = qvc.App._http_get_json("http://example.invalid/HOST_INFO")
            self.assertIsNone(result)
            self.assertEqual(mock_requests.get.call_count, 1)
            _, kwargs = mock_requests.get.call_args
            self.assertIn("timeout", kwargs)
            self.assertGreater(kwargs["timeout"], 0)

    def test_http_get_json_returns_none_on_non_200(self) -> None:
        with patch.object(qvc, "requests") as mock_requests:
            mock_requests.RequestException = Exception
            response = MagicMock()
            response.status_code = 404
            mock_requests.get.return_value = response
            self.assertIsNone(qvc.App._http_get_json("http://x/y"))

    def test_http_get_json_returns_none_on_bad_json(self) -> None:
        with patch.object(qvc, "requests") as mock_requests:
            mock_requests.RequestException = Exception
            response = MagicMock()
            response.status_code = 200
            response.json.side_effect = ValueError("not json")
            mock_requests.get.return_value = response
            self.assertIsNone(qvc.App._http_get_json("http://x/y"))

    def test_fetch_node_value_parses_VALUE_array(self) -> None:
        svc = SimpleNamespace(addresses=[bytes([127, 0, 0, 1])], port=8080)
        with patch.object(qvc.App, "_http_get_json", return_value={"VALUE": [1.75]}):
            self.assertEqual(qvc.App._fetch_node_value(svc, "/avatar/eyeheight"), 1.75)

    def test_fetch_node_value_handles_missing_VALUE(self) -> None:
        svc = SimpleNamespace(addresses=[bytes([127, 0, 0, 1])], port=8080)
        with patch.object(qvc.App, "_http_get_json", return_value={"FULL_PATH": "/x"}):
            self.assertIsNone(qvc.App._fetch_node_value(svc, "/x"))

    def test_fetch_host_info_extracts_NAME(self) -> None:
        svc = SimpleNamespace(addresses=[bytes([127, 0, 0, 1])], port=8080)
        with patch.object(qvc.App, "_http_get_json", return_value={"NAME": "VRChat-Client-XYZ"}):
            hi = qvc.App._fetch_host_info(svc)
            self.assertIsNotNone(hi)
            self.assertEqual(hi.name, "VRChat-Client-XYZ")  # type: ignore[union-attr]

    def test_on_close_releases_browser(self) -> None:
        """Closing the window must close the long-lived Zeroconf instance."""
        fake_browser = MagicMock()
        self.app._browser = fake_browser
        # Replace root.destroy so the test framework can still tear down.
        with patch.object(self.app.root, "destroy"):
            self.app._on_close()
        fake_browser.zc.close.assert_called_once()
        self.assertIsNone(self.app._browser)


if __name__ == "__main__":
    unittest.main()

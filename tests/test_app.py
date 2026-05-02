"""Unit tests for QuickVRCScaler.

Covers the pure UI-state logic: warning generation and incoming-OSC event
handling. Network start-up (OSC server, OSCQuery) is patched out so tests
do not bind sockets or touch zeroconf.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
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
        cls._orig_load_default = qvc.App._load_default_height
        cls._orig_udp_client = qvc.udp_client.SimpleUDPClient
        qvc.App._start_server = lambda self: None  # type: ignore[assignment]
        qvc.App._start_oscquery = lambda self: None  # type: ignore[assignment]
        qvc.App._poll_oscquery = lambda self: None  # type: ignore[assignment]
        qvc.App._load_default_height = classmethod(lambda cls: qvc.DEFAULT_HEIGHT)  # type: ignore[method-assign]
        qvc.udp_client.SimpleUDPClient = lambda *a, **kw: MagicMock()  # type: ignore[assignment]

    @classmethod
    def tearDownClass(cls) -> None:
        qvc.App._start_server = cls._orig_start_server  # type: ignore[assignment]
        qvc.App._start_oscquery = cls._orig_start_query  # type: ignore[assignment]
        qvc.App._poll_oscquery = cls._orig_poll_query  # type: ignore[assignment]
        qvc.App._load_default_height = cls._orig_load_default  # type: ignore[method-assign]
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

    def test_default_world_limits_do_not_warn(self) -> None:
        self.app.cur_min = qvc.WORLD_DEFAULT_MIN
        self.app.cur_max = qvc.WORLD_DEFAULT_MAX
        self.app._refresh_warning(pending=10.0)
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

    def test_allowed_event_parses_false_string(self) -> None:
        self.app._apply_event("allowed", "False")
        self.assertIs(self.app.cur_allowed, False)
        self.assertIn("BLOCKED", self.app.info_allowed_var.get())

    def test_allowed_event_normalizes_case_and_whitespace(self) -> None:
        self.app._apply_event("allowed", " YES ")
        self.assertIs(self.app.cur_allowed, True)
        self.app._apply_event("allowed", "Off")
        self.assertIs(self.app.cur_allowed, False)

    def test_allowed_event_parses_numeric_values(self) -> None:
        self.app._apply_event("allowed", 0)
        self.assertIs(self.app.cur_allowed, False)
        self.app._apply_event("allowed", 1)
        self.assertIs(self.app.cur_allowed, True)

    def test_unknown_allowed_value_is_ignored(self) -> None:
        self.app.cur_allowed = True
        self.app.info_allowed_var.set("Scaling:     allowed")
        self.app._apply_event("allowed", "definitely")
        self.assertIs(self.app.cur_allowed, True)
        self.assertEqual(self.app.info_allowed_var.get(), "Scaling:     allowed")

    def test_status_event_updates_status_without_touching_warning(self) -> None:
        self.app.warning_var.set("keep this warning")
        self.app._apply_event("status", "OSCQuery: test status")
        self.assertIn("OSCQuery: test status", self.app.status_var.get())
        self.assertEqual(self.app.warning_var.get(), "keep this warning")

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
        self.assertAlmostEqual(args[1], qvc.DEFAULT_HEIGHT)

    def test_reset_uses_saved_default(self) -> None:
        self.app.default_height = 1.51
        self.app._update_reset_menu()
        self.app._reset()
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], 1.51)

    def test_set_current_as_default_saves_and_updates_reset_label(self) -> None:
        self.app.cur_height = 1.51
        with patch.object(qvc.App, "_save_default_height", return_value=(True, None)) as mock_save:
            self.app._set_current_as_default()
        mock_save.assert_called_once_with(1.51)
        self.assertAlmostEqual(self.app.default_height, 1.51)
        self.assertIn("1.51", self.app.reset_text_var.get())
        self.assertIn("Default saved", self.app.status_var.get())

    def test_set_current_as_default_reports_save_failure(self) -> None:
        self.app.cur_height = 1.51
        with patch.object(qvc.App, "_save_default_height", return_value=(False, "denied")):
            self.app._set_current_as_default()
        self.assertAlmostEqual(self.app.default_height, qvc.DEFAULT_HEIGHT)
        self.assertIn("Default not saved", self.app.status_var.get())

    def test_entry_clamps_to_absolute_bounds(self) -> None:
        self.app.entry_var.set("99999")
        self.app._on_entry_submit()
        args, _ = self.app.client.send_message.call_args
        self.assertLessEqual(args[1], qvc.VRCHAT_ABSOLUTE_MAX)

    def test_entry_rejects_non_numeric(self) -> None:
        self.app.entry_var.set("tall")
        self.app._on_entry_submit()
        self.app.client.send_message.assert_not_called()
        self.assertIn("not a number", self.app.warning_var.get())

    def test_send_height_surfaces_unexpected_send_error(self) -> None:
        self.app.client.send_message.side_effect = RuntimeError("simulated send failure")
        self.app._send_height(2.0)
        self.assertIn("Send failed", self.app.warning_var.get())
        self.assertIn("simulated send failure", self.app.warning_var.get())


class LifecycleTests(_BaseAppTest):
    def test_on_close_shuts_down_server_and_closes_socket(self) -> None:
        server = MagicMock()
        self.app.server = server
        with patch.object(self.app.root, "destroy"):
            self.app._on_close()
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        self.assertIsNone(self.app.server)
        self.assertTrue(self.app._closing)

    def test_on_close_closes_socket_even_if_shutdown_fails(self) -> None:
        server = MagicMock()
        server.shutdown.side_effect = RuntimeError("simulated shutdown race")
        self.app.server = server
        with patch.object(self.app.root, "destroy"):
            self.app._on_close()
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()

    def test_on_close_is_idempotent(self) -> None:
        server = MagicMock()
        self.app.server = server
        with patch.object(self.app.root, "destroy"):
            self.app._on_close()
            self.app._on_close()
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()

    def test_on_close_continues_if_browser_close_fails(self) -> None:
        browser = MagicMock()
        browser.zc.close.side_effect = RuntimeError("simulated zeroconf race")
        self.app._browser = browser
        with patch.object(self.app.root, "destroy") as mock_destroy:
            self.app._on_close()
        browser.zc.close.assert_called_once()
        mock_destroy.assert_called_once()
        self.assertIsNone(self.app._browser)

    def test_drain_events_does_not_reschedule_when_closing(self) -> None:
        self.app._closing = True
        with patch.object(self.app.root, "after") as mock_after:
            self.app._drain_events()
        mock_after.assert_not_called()

    def test_drain_events_leaves_pending_events_when_closing(self) -> None:
        self.app.events.put(("height", 2.0))
        self.app._closing = True
        with patch.object(self.app, "_apply_event") as mock_apply_event:
            self.app._drain_events()
        mock_apply_event.assert_not_called()
        self.assertEqual(self.app.events.qsize(), 1)

    def test_poll_does_not_spawn_or_reschedule_when_closing(self) -> None:
        real_poll = type(self)._orig_poll_query
        self.app._closing = True
        with patch.object(qvc.threading, "Thread") as mock_thread, \
             patch.object(self.app.root, "after") as mock_after:
            real_poll(self.app)
        mock_thread.assert_not_called()
        mock_after.assert_not_called()

    def test_worker_clears_in_flight_when_closing(self) -> None:
        self.app._closing = True
        self.app._poll_in_flight = True
        self.app._poll_oscquery_worker()
        self.assertFalse(self.app._poll_in_flight)


class HeightHelperTests(_BaseAppTest):
    def test_initial_slider_falls_back_to_default_height(self) -> None:
        self.assertAlmostEqual(self.app._current_height(), qvc.DEFAULT_HEIGHT)
        self.assertLess(self.app.slider_var.get(), qvc.SLIDER_MAX)

    def test_custom_default_initializes_slider_and_reset_label(self) -> None:
        with patch.object(qvc.App, "_load_default_height", return_value=1.51):
            root, app = _make_app()
        try:
            self.assertAlmostEqual(app._current_height(), 1.51)
            self.assertIn("1.51", app.reset_text_var.get())
        finally:
            root.destroy()

    def test_clamp_absolute_height_uses_vrchat_bounds(self) -> None:
        self.assertEqual(qvc.App._clamp_absolute_height(-1.0), qvc.VRCHAT_ABSOLUTE_MIN)
        self.assertEqual(qvc.App._clamp_absolute_height(99999.0), qvc.VRCHAT_ABSOLUTE_MAX)
        self.assertEqual(qvc.App._clamp_absolute_height(1.6), 1.6)

    def test_set_display_height_maps_full_range_but_shows_true_value(self) -> None:
        self.app._set_display_height(qvc.VRCHAT_ABSOLUTE_MAX)
        self.assertAlmostEqual(self.app.slider_var.get(), qvc.SLIDER_MAX)
        self.assertEqual(self.app.height_label.cget("text"), "10000.00 m")

    def test_set_display_height_maps_recommended_values_inside_slider(self) -> None:
        self.app._set_display_height(25.0)
        self.assertGreater(self.app.slider_var.get(), qvc.SLIDER_MIN)
        self.assertLess(self.app.slider_var.get(), qvc.SLIDER_MAX)
        self.assertEqual(self.app.height_label.cget("text"), "25.00 m")

    def test_set_display_height_does_not_echo_to_osc(self) -> None:
        self.app._set_display_height(2.0)
        self.app.client.send_message.assert_not_called()

    def test_set_display_height_restores_suppression_after_error(self) -> None:
        with patch.object(self.app.slider_var, "set", side_effect=tk.TclError("boom")):
            with self.assertRaises(tk.TclError):
                self.app._set_display_height(2.0)
        self.assertFalse(self.app._suppress_send)

    def test_slider_mapping_hits_key_range_boundaries(self) -> None:
        self.assertAlmostEqual(self.app._slider_position_to_height(0.0), qvc.VRCHAT_ABSOLUTE_MIN)
        self.assertAlmostEqual(self.app._slider_position_to_height(qvc.SLIDER_LOW_FRACTION), qvc.SAFE_MIN)
        recommended_end = qvc.SLIDER_LOW_FRACTION + qvc.SLIDER_RECOMMENDED_FRACTION
        self.assertAlmostEqual(self.app._slider_position_to_height(recommended_end), qvc.SAFE_MAX)
        self.assertAlmostEqual(self.app._slider_position_to_height(1.0), qvc.VRCHAT_ABSOLUTE_MAX)

    def test_slider_position_round_trips_height(self) -> None:
        for height in (0.01, 0.05, 0.1, 1.0, 100.0, 1000.0, 10000.0):
            position = self.app._height_to_slider_position(height)
            self.assertAlmostEqual(self.app._slider_position_to_height(position), height)

    def test_slider_click_maps_to_pointer_position(self) -> None:
        self.app.slider.coords = MagicMock(side_effect=[(10,), (210,)])  # type: ignore[method-assign]
        position = self.app._slider_event_position(SimpleNamespace(x=110))
        self.assertAlmostEqual(position, 0.5)

    def test_slider_click_clamps_outside_track(self) -> None:
        self.app.slider.coords = MagicMock(side_effect=[(10,), (210,)])  # type: ignore[method-assign]
        self.assertEqual(self.app._slider_event_position(SimpleNamespace(x=-50)), qvc.SLIDER_MIN)
        self.app.slider.coords = MagicMock(side_effect=[(10,), (210,)])  # type: ignore[method-assign]
        self.assertEqual(self.app._slider_event_position(SimpleNamespace(x=999)), qvc.SLIDER_MAX)

    def test_slider_pointer_sends_clicked_height_not_extreme(self) -> None:
        self.app.slider.coords = MagicMock(side_effect=[(10,), (210,)])  # type: ignore[method-assign]
        result = self.app._on_slider_pointer(SimpleNamespace(x=110))
        self.assertEqual(result, "break")
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], self.app._slider_position_to_height(0.5))
        self.assertNotAlmostEqual(args[1], qvc.VRCHAT_ABSOLUTE_MIN)
        self.assertNotAlmostEqual(args[1], qvc.VRCHAT_ABSOLUTE_MAX)


class StatusTests(_BaseAppTest):
    def test_start_oscquery_reports_unavailable_when_dependency_missing(self) -> None:
        real_start_query = type(self)._orig_start_query
        with patch.object(qvc, "_OSCQUERY_AVAILABLE", False):
            real_start_query(self.app)
        self.assertIn("OSCQuery unavailable", self.app.status_var.get())

    def test_queue_status_drops_late_updates_after_close(self) -> None:
        self.app._closing = True
        self.app._queue_status("OSCQuery: too late")
        self.assertTrue(self.app.events.empty())

    def test_warning_changes_do_not_clobber_status(self) -> None:
        self.app._set_status("OSCQuery: keep this")
        self.app.entry_var.set("tall")
        self.app._on_entry_submit()
        self.assertIn("OSCQuery: keep this", self.app.status_var.get())


class SettingsTests(unittest.TestCase):
    def test_load_default_height_reads_saved_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, qvc.SETTINGS_FILENAME)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"default_height": 1.51}, f)
            with patch.object(qvc.App, "_settings_path", return_value=qvc.Path(path)):
                self.assertAlmostEqual(qvc.App._load_default_height(), 1.51)

    def test_load_default_height_falls_back_on_bad_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, qvc.SETTINGS_FILENAME)
            with open(path, "w", encoding="utf-8") as f:
                f.write("not json")
            with patch.object(qvc.App, "_settings_path", return_value=qvc.Path(path)):
                self.assertAlmostEqual(qvc.App._load_default_height(), qvc.DEFAULT_HEIGHT)

    def test_save_default_height_writes_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", qvc.SETTINGS_FILENAME)
            with patch.object(qvc.App, "_settings_path", return_value=qvc.Path(path)):
                ok, error = qvc.App._save_default_height(1.51)
            self.assertTrue(ok)
            self.assertIsNone(error)
            with open(path, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"default_height": 1.51})


class QuickButtonTests(_BaseAppTest):
    def test_apply_preset_sends_exact_value(self) -> None:
        self.app._apply_preset(2.0)
        args, _ = self.app.client.send_message.call_args
        self.assertEqual(args[0], qvc.ADDR_HEIGHT)
        self.assertAlmostEqual(args[1], 2.0)

    def test_apply_preset_clamps_high(self) -> None:
        self.app._apply_preset(99999.0)
        args, _ = self.app.client.send_message.call_args
        self.assertLessEqual(args[1], qvc.VRCHAT_ABSOLUTE_MAX)

    def test_apply_preset_clamps_low(self) -> None:
        self.app._apply_preset(0.0)
        args, _ = self.app.client.send_message.call_args
        self.assertGreaterEqual(args[1], qvc.VRCHAT_ABSOLUTE_MIN)

    def test_apply_preset_full_range_value_still_sent(self) -> None:
        self.app._apply_preset(qvc.VRCHAT_ABSOLUTE_MAX)
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], qvc.VRCHAT_ABSOLUTE_MAX)
        self.assertAlmostEqual(self.app.slider_var.get(), qvc.SLIDER_MAX)

    def test_apply_scale_uses_reported_height(self) -> None:
        self.app.cur_height = 1.6
        self.app._apply_scale(2.0)
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], 3.2)

    def test_apply_scale_falls_back_to_slider(self) -> None:
        self.app.cur_height = None
        self.app._set_display_height(2.0)
        self.app._apply_scale(0.5)
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], 1.0)

    def test_slider_can_send_absolute_min(self) -> None:
        self.app.slider_var.set(qvc.SLIDER_MIN)
        self.app._on_slider(str(qvc.SLIDER_MIN))
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], qvc.VRCHAT_ABSOLUTE_MIN)

    def test_slider_can_send_absolute_max(self) -> None:
        self.app.slider_var.set(qvc.SLIDER_MAX)
        self.app._on_slider(str(qvc.SLIDER_MAX))
        args, _ = self.app.client.send_message.call_args
        self.assertAlmostEqual(args[1], qvc.VRCHAT_ABSOLUTE_MAX)

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

    def test_browser_failure_queues_status_event(self) -> None:
        with patch.object(qvc, "OSCQueryBrowser", side_effect=RuntimeError("boom")):
            self.app._poll_oscquery_once()
        self.assertEqual(self.app.events.get_nowait(), ("status", "OSCQuery browse failed: boom"))

    def test_no_vrchat_service_queues_status_event(self) -> None:
        with patch.object(qvc, "OSCQueryBrowser") as mock_browser_cls, \
             patch.object(qvc.time, "sleep"):
            mock_browser_cls.return_value.get_discovered_oscquery.return_value = []
            self.app._poll_oscquery_once()
        self.assertEqual(
            self.app.events.get_nowait(),
            ("status", "OSCQuery: no VRChat service found"),
        )

    def test_successful_poll_queues_refresh_status_event(self) -> None:
        svc = object()
        with patch.object(qvc, "OSCQueryBrowser") as mock_browser_cls, \
             patch.object(qvc.time, "sleep"), \
             patch.object(qvc.time, "strftime", return_value="12:34:56"), \
             patch.object(qvc.App, "_fetch_host_info", return_value=SimpleNamespace(name="VRChat")), \
             patch.object(qvc.App, "_fetch_node_value", return_value=1.0):
            mock_browser_cls.return_value.get_discovered_oscquery.return_value = [svc]
            self.app._poll_oscquery_once()
        events = []
        while not self.app.events.empty():
            events.append(self.app.events.get_nowait())
        self.assertIn(("status", "OSCQuery: refreshed 12:34:56"), events)

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

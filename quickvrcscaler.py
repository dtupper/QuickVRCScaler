"""QuickVRCScaler — small OSC client for VRChat avatar scaling.

Sends to 127.0.0.1:9000 (VRChat input) and listens on 127.0.0.1:9001
(VRChat output). Endpoints per https://docs.vrchat.com/docs/osc-avatar-scaling
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

from pythonosc import udp_client
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

try:
    from tinyoscquery.queryservice import OSCQueryService
    from tinyoscquery.query import OSCQueryBrowser
    from tinyoscquery.utility import get_open_tcp_port, get_open_udp_port
    import requests  # used directly so we can pass an HTTP timeout
    _OSCQUERY_AVAILABLE = True
except Exception:
    _OSCQUERY_AVAILABLE = False
    requests = None  # type: ignore[assignment]

VRCHAT_HOST = "127.0.0.1"
SEND_PORT = 9000
LISTEN_PORT = 9001
BASE_STATUS = f"Send → {VRCHAT_HOST}:{SEND_PORT}    Listen ← :{LISTEN_PORT}"

ADDR_HEIGHT = "/avatar/eyeheight"
ADDR_MIN = "/avatar/eyeheightmin"
ADDR_MAX = "/avatar/eyeheightmax"
ADDR_ALLOWED = "/avatar/eyeheightscalingallowed"

SAFE_MIN = 0.1
SAFE_MAX = 100.0
VRCHAT_ABSOLUTE_MIN = 0.01
VRCHAT_ABSOLUTE_MAX = 10000.0
WORLD_DEFAULT_MIN = 0.2
WORLD_DEFAULT_MAX = 5.0
DEFAULT_HEIGHT = 1.6
SLIDER_MIN = 0.0
SLIDER_MAX = 1.0
SLIDER_LOW_FRACTION = 0.15
SLIDER_RECOMMENDED_FRACTION = 0.65
SLIDER_HIGH_FRACTION = 0.20

# OSCQuery polling
POLL_INTERVAL_MS = 10_000
# tinyoscquery's built-in HTTP client doesn't pass a timeout, so a half-dead
# OSCQuery peer can hang the worker thread forever. We do our own requests.get
# calls with this timeout to bound that.
OSCQUERY_HTTP_TIMEOUT = 2.0
# Initial mDNS discovery needs a beat before any services show up.
OSCQUERY_INITIAL_DISCOVERY_DELAY = 0.8
SETTINGS_FILENAME = "settings.json"


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("QuickVRCScaler")
        root.geometry("540x640")
        root.minsize(540, 640)

        self.client = udp_client.SimpleUDPClient(VRCHAT_HOST, SEND_PORT)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.default_height = self._load_default_height()

        # State reported by VRChat
        self.cur_height: float | None = None
        self.cur_min: float | None = None
        self.cur_max: float | None = None
        self.cur_allowed: bool | None = None

        # Suppress send when slider is updated programmatically from incoming OSC
        self._suppress_send = False

        # OSCQuery polling state. The browser is long-lived because mDNS
        # discovery is meant to be continuous — recreating it per poll leaks
        # zeroconf threads and sockets, which after a couple of hours starves
        # the GIL enough to freeze the UI. The in-flight flag keeps a stalled
        # worker from causing follow-up workers to pile up behind it.
        self._browser: "OSCQueryBrowser | None" = None
        self._poll_in_flight = False
        self._closing = False
        self.server: ThreadingOSCUDPServer | None = None

        self._build_ui()
        self._start_server()
        self._start_oscquery()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._drain_events)
        # Try to actively pull values shortly after startup, then keep polling.
        self.root.after(1500, self._poll_oscquery)

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        header = ttk.Label(
            self.root, text="VRChat Avatar Scaler", font=("Segoe UI", 14, "bold")
        )
        header.pack(anchor="w", **pad)

        # Slider row
        slider_frame = ttk.Frame(self.root)
        slider_frame.pack(fill="x", **pad)

        self.slider_var = tk.DoubleVar(value=self._height_to_slider_position(self.default_height))
        self.slider = ttk.Scale(
            slider_frame,
            from_=SLIDER_MIN,
            to=SLIDER_MAX,
            orient="horizontal",
            variable=self.slider_var,
            command=self._on_slider,
        )
        # ttk.Scale trough clicks jump by a theme-defined step, which is a bad
        # fit for our non-linear 0.01–10000 m mapping. Map pointer position
        # ourselves so a click lands where the user clicked.
        self.slider.bind("<Button-1>", self._on_slider_pointer)
        self.slider.bind("<B1-Motion>", self._on_slider_pointer)
        self.slider.pack(side="left", fill="x", expand=True)

        self.height_label = ttk.Label(
            slider_frame,
            text=f"{self.default_height:.2f} m",
            width=8,
            anchor="e",
            font=("Segoe UI", 11),
        )
        self.height_label.pack(side="right", padx=(8, 0))

        # Manual entry row
        entry_frame = ttk.Frame(self.root)
        entry_frame.pack(fill="x", **pad)
        ttk.Label(entry_frame, text="Set exact (m):").pack(side="left")
        self.entry_var = tk.StringVar()
        entry = ttk.Entry(entry_frame, textvariable=self.entry_var, width=10)
        entry.pack(side="left", padx=(6, 6))
        entry.bind("<Return>", self._on_entry_submit)
        ttk.Button(entry_frame, text="Apply", command=self._on_entry_submit).pack(
            side="left"
        )
        self.reset_text_var = tk.StringVar()
        self.reset_menu = tk.Menu(entry_frame, tearoff=False)
        self.reset_menu.add_command(command=self._reset)
        self.reset_menu.add_command(label="Set current height as default", command=self._set_current_as_default)
        reset_button = ttk.Menubutton(entry_frame, textvariable=self.reset_text_var)
        reset_button["menu"] = self.reset_menu
        reset_button.pack(side="right")
        self._update_reset_menu()
        ttk.Button(entry_frame, text="Refresh", command=self._poll_oscquery).pack(
            side="right", padx=(0, 4)
        )

        # Warning banner
        self.warning_var = tk.StringVar(value="")
        self.warning = tk.Label(
            self.root,
            textvariable=self.warning_var,
            fg="#7a4a00",
            bg="#fff3cd",
            anchor="w",
            justify="left",
            wraplength=410,
        )
        # Pack lazily when content present.

        # Info readouts
        info = ttk.LabelFrame(self.root, text="Reported by VRChat")
        info.pack(fill="both", expand=True, padx=12, pady=(8, 12))

        self.info_height_var = tk.StringVar(value="Eye height:  —")
        self.info_min_var = tk.StringVar(value="World min:   —")
        self.info_max_var = tk.StringVar(value="World max:   —")
        self.info_allowed_var = tk.StringVar(value="Scaling:     —")

        for var in (
            self.info_height_var,
            self.info_min_var,
            self.info_max_var,
            self.info_allowed_var,
        ):
            ttk.Label(info, textvariable=var, font=("Consolas", 10)).pack(
                anchor="w", padx=10, pady=2
            )

        self.status_var = tk.StringVar(value=f"{BASE_STATUS}    OSCQuery: starting")
        status = ttk.Label(self.root, textvariable=self.status_var, foreground="#666")
        status.pack(anchor="w", padx=12, pady=(0, 4))

        # --- Quick set (VR-overlay friendly chunky buttons) ----------------
        try:
            ttk.Style().configure(
                "Quick.TButton", font=("Segoe UI", 13, "bold"), padding=(8, 14)
            )
        except tk.TclError:
            pass

        quick = ttk.LabelFrame(self.root, text="Quick set")
        quick.pack(fill="x", padx=12, pady=(0, 10))

        # 4 columns × 4 rows, each cell stretches via column/rowconfigure.
        presets = [
            ("0.01 m", 0.01), ("0.05 m", 0.05), ("0.1 m", 0.1), ("1 m", 1.0),
            ("5 m", 5.0), ("25 m", 25.0), ("100 m", 100.0), ("10k m", 10000.0),
        ]
        adjust = [
            ("-50%", 0.5), ("-25%", 0.75), ("-10%", 0.9), ("÷2", 0.5),
            ("×2", 2.0), ("+10%", 1.1), ("+25%", 1.25), ("+50%", 1.5),
        ]

        grid_frame = ttk.Frame(quick)
        grid_frame.pack(fill="both", expand=True, padx=6, pady=8)
        for c in range(4):
            grid_frame.columnconfigure(c, weight=1, uniform="quick")

        for i, (label, h) in enumerate(presets):
            ttk.Button(
                grid_frame, text=label, style="Quick.TButton",
                command=lambda v=h: self._apply_preset(v),
            ).grid(row=i // 4, column=i % 4, sticky="nsew", padx=3, pady=3)

        for i, (label, factor) in enumerate(adjust):
            ttk.Button(
                grid_frame, text=label, style="Quick.TButton",
                command=lambda f=factor: self._apply_scale(f),
            ).grid(row=2 + i // 4, column=i % 4, sticky="nsew", padx=3, pady=3)

        ttk.Label(
            quick,
            text="Slider covers 0.01–10000 m; most travel favors the supported 0.1–100 m range.",
            foreground="#666",
            wraplength=500,
        ).pack(anchor="w", padx=8, pady=(0, 8))

    # --- OSC server -------------------------------------------------------

    def _start_server(self) -> None:
        dispatcher = Dispatcher()
        dispatcher.map(ADDR_HEIGHT, self._handle_osc, "height")
        dispatcher.map(ADDR_MIN, self._handle_osc, "min")
        dispatcher.map(ADDR_MAX, self._handle_osc, "max")
        dispatcher.map(ADDR_ALLOWED, self._handle_osc, "allowed")

        try:
            self.server = ThreadingOSCUDPServer(
                ("127.0.0.1", LISTEN_PORT), dispatcher
            )
        except OSError as exc:
            self.warning_var.set(f"Could not bind UDP {LISTEN_PORT}: {exc}")
            self.warning.pack(fill="x", padx=12, pady=(0, 6))
            return

        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

    # --- OSCQuery (active read of read-only endpoints) -------------------

    def _start_oscquery(self) -> None:
        """Advertise an OSCQuery service so VRChat pushes current avatar state.

        VRChat only emits /avatar/eyeheight* on change events. By registering
        as an OSCQuery service that subscribes to /avatar, VRChat will send
        the current values to our listen port on connect, populating readouts
        without waiting for the user to change avatars.
        """
        self.oscquery = None
        if not _OSCQUERY_AVAILABLE:
            self._set_status("OSCQuery unavailable")
            return
        try:
            http_port = get_open_tcp_port()
            self.oscquery = OSCQueryService(
                "QuickVRCScaler", http_port, LISTEN_PORT
            )
            # Advertise interest in the avatar scaling endpoints so VRChat
            # mirrors them to us on connection.
            for addr in (ADDR_HEIGHT, ADDR_MIN, ADDR_MAX, ADDR_ALLOWED):
                try:
                    self.oscquery.advertise_endpoint(addr, access="readwrite")
                except Exception:
                    pass
            self._set_status("OSCQuery advertised")
        except Exception as exc:
            self.oscquery = None
            self._set_status(f"OSCQuery unavailable: {exc}")

    def _set_status(self, detail: str) -> None:
        self.status_var.set(f"{BASE_STATUS}    {detail}")

    def _queue_status(self, detail: str) -> None:
        if not self._closing:
            self.events.put(("status", detail))

    def _poll_oscquery(self) -> None:
        """Kick off (at most one) OSCQuery poll worker, then reschedule.

        The in-flight guard matters: without it a stalled HTTP request would
        let workers accumulate every 10 s, and each one keeps zeroconf and
        the GIL busy enough to eventually freeze the UI.
        """
        if self._closing:
            return
        if _OSCQUERY_AVAILABLE and not self._poll_in_flight:
            self._poll_in_flight = True
            threading.Thread(target=self._poll_oscquery_worker, daemon=True).start()
        self.root.after(POLL_INTERVAL_MS, self._poll_oscquery)

    def _poll_oscquery_worker(self) -> None:
        try:
            self._poll_oscquery_once()
        finally:
            # Always clear the flag, even on unexpected exceptions, so we
            # don't permanently stop polling because of a single bad cycle.
            self._poll_in_flight = False

    def _poll_oscquery_once(self) -> None:
        """Run one poll cycle: enumerate discovered services, pull values."""
        if self._closing:
            return
        # Reuse a single OSCQueryBrowser for the lifetime of the app. The
        # underlying zeroconf instance keeps discovering services in the
        # background — we just read what it has each tick.
        if self._browser is None:
            try:
                self._browser = OSCQueryBrowser()
            except Exception as exc:
                self._queue_status(f"OSCQuery browse failed: {exc}")
                return
            # Give the very first browse a moment to populate. Subsequent
            # polls hit a warm browser and skip this delay entirely.
            time.sleep(OSCQUERY_INITIAL_DISCOVERY_DELAY)

        try:
            services = list(self._browser.get_discovered_oscquery())
        except Exception as exc:
            self._queue_status(f"OSCQuery enumerate failed: {exc}")
            return

        # Build (svc, host_info, _) triples so _pick_vrchat_service can
        # do name-based preference (and skip e.g. VRCFT on the same host).
        candidates = []
        for svc in services:
            host_info = self._fetch_host_info(svc)
            if host_info is None:
                continue
            candidates.append((svc, host_info, None))

        chosen = self._pick_vrchat_service(candidates)
        if chosen is None:
            self._queue_status("OSCQuery: no VRChat service found")
            return

        for addr, key in (
            (ADDR_HEIGHT, "height"),
            (ADDR_MIN, "min"),
            (ADDR_MAX, "max"),
            (ADDR_ALLOWED, "allowed"),
        ):
            value = self._fetch_node_value(chosen, addr)
            if value is None:
                continue
            self.events.put((key, value))
        self._queue_status(f"OSCQuery: refreshed {time.strftime('%H:%M:%S')}")

    @staticmethod
    def _http_get_json(url: str):
        """GET `url` and return parsed JSON, or None on any failure.

        The whole point of this helper is to enforce a timeout on every
        OSCQuery HTTP call — tinyoscquery's own client does not, so a
        half-dead peer would otherwise hang the worker thread forever.
        """
        if requests is None:
            return None
        try:
            r = requests.get(url, timeout=OSCQUERY_HTTP_TIMEOUT)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    @staticmethod
    def _service_url(service_info, path: str):
        """Build the OSCQuery HTTP URL for a zeroconf ServiceInfo, or None."""
        try:
            ip = ".".join(str(int(b)) for b in service_info.addresses[0])
            port = service_info.port
        except Exception:
            return None
        return f"http://{ip}:{port}{path}"

    @classmethod
    def _fetch_host_info(cls, service_info):
        url = cls._service_url(service_info, "/HOST_INFO")
        if url is None:
            return None
        data = cls._http_get_json(url)
        if not isinstance(data, dict):
            return None
        return SimpleNamespace(name=data.get("NAME") or "")

    @classmethod
    def _fetch_node_value(cls, service_info, address: str):
        url = cls._service_url(service_info, address)
        if url is None:
            return None
        data = cls._http_get_json(url)
        if not isinstance(data, dict):
            return None
        value = data.get("VALUE")
        if isinstance(value, list) and value:
            return value[0]
        return None

    @staticmethod
    def _pick_vrchat_service(candidates):
        """Choose the best OSCQuery service from a list of (svc, host_info, _) triples.

        When multiple OSCQuery apps are running (e.g. VRCFT alongside VRChat),
        prefer the one whose advertised host name identifies it as VRChat.
        Falls back to the first candidate if no name matches.
        """
        if not candidates:
            return None

        def vrchat_priority(triple) -> int:
            host_info = triple[1] if len(triple) > 1 else None
            name = (getattr(host_info, "name", "") or "").lower()
            return 0 if "vrchat" in name else 1

        return sorted(candidates, key=vrchat_priority)[0][0]

    @staticmethod
    def _parse_bool(value: object) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1", "yes", "on"):
                return True
            if normalized in ("false", "0", "no", "off"):
                return False
        return None

    @staticmethod
    def _settings_path() -> Path:
        base = Path(os.environ["APPDATA"]) if "APPDATA" in os.environ else Path.home()
        return base / "QuickVRCScaler" / SETTINGS_FILENAME

    @classmethod
    def _load_default_height(cls) -> float:
        try:
            with cls._settings_path().open("r", encoding="utf-8") as f:
                data = json.load(f)
            return cls._clamp_absolute_height(float(data["default_height"]))
        except Exception:
            return DEFAULT_HEIGHT

    @classmethod
    def _save_default_height(cls, h: float) -> tuple[bool, str | None]:
        try:
            path = cls._settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as f:
                json.dump({"default_height": h}, f)
        except Exception as exc:
            return False, str(exc)
        return True, None

    def _on_close(self) -> None:
        """Best-effort cleanup of background discovery before the window dies.

        Closes the UDP server and long-lived zeroconf instance so their
        threads and sockets are released promptly. Wrapped in try/except
        because shutdown races with mainloop teardown and we'd rather log
        nothing than crash on the way out.
        """
        if self._closing:
            return
        self._closing = True

        server = self.server
        self.server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass

        browser = self._browser
        self._browser = None
        if browser is not None:
            try:
                browser.zc.close()
            except Exception:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _handle_osc(self, _addr: str, key: str, *args) -> None:
        if not args:
            return
        # OSC handler runs on server thread; hand off to UI thread.
        self.events.put((key, args[0]))

    def _drain_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                key, value = self.events.get_nowait()
                self._apply_event(key, value)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_events)

    def _apply_event(self, key: str, value: object) -> None:
        if key == "height":
            try:
                h = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return
            self.cur_height = h
            self.info_height_var.set(f"Eye height:  {h:.3f} m")
            self._set_display_height(h)
        elif key == "min":
            try:
                self.cur_min = float(value)  # type: ignore[arg-type]
                self.info_min_var.set(f"World min:   {self.cur_min:.3f} m")
            except (TypeError, ValueError):
                return
        elif key == "max":
            try:
                self.cur_max = float(value)  # type: ignore[arg-type]
                self.info_max_var.set(f"World max:   {self.cur_max:.3f} m")
            except (TypeError, ValueError):
                return
        elif key == "allowed":
            allowed = self._parse_bool(value)
            if allowed is None:
                return
            self.cur_allowed = allowed
            self.info_allowed_var.set(
                f"Scaling:     {'allowed' if self.cur_allowed else 'BLOCKED by world/Udon'}"
            )
        elif key == "status":
            self._set_status(str(value))
            return
        self._refresh_warning()

    # --- UI actions -------------------------------------------------------

    def _on_slider(self, _value: str) -> None:
        h = self._slider_position_to_height(float(self.slider_var.get()))
        self.height_label.configure(text=f"{h:.2f} m")
        if self._suppress_send:
            return
        self._send_height(h)

    def _on_slider_pointer(self, event) -> str:
        position = self._slider_event_position(event)
        self.slider_var.set(position)
        self._on_slider(str(position))
        return "break"

    def _slider_event_position(self, event) -> float:
        try:
            min_x = float(self.slider.coords(SLIDER_MIN)[0])
            max_x = float(self.slider.coords(SLIDER_MAX)[0])
        except (tk.TclError, IndexError, TypeError, ValueError):
            min_x = 0.0
            max_x = float(max(1, self.slider.winfo_width()))
        if max_x <= min_x:
            return SLIDER_MIN
        fraction = (float(event.x) - min_x) / (max_x - min_x)
        return max(SLIDER_MIN, min(SLIDER_MAX, fraction))

    def _on_entry_submit(self, *_args) -> None:
        raw = self.entry_var.get().strip()
        if not raw:
            return
        try:
            h = float(raw)
        except ValueError:
            self.warning_var.set(f"'{raw}' is not a number.")
            self.warning.pack(fill="x", padx=12, pady=(0, 6))
            return
        h = self._clamp_absolute_height(h)
        self._set_display_height(h)
        self._send_height(h)

    def _reset(self) -> None:
        self._apply_preset(self.default_height)

    def _update_reset_menu(self) -> None:
        label = f"Reset {self.default_height:.2f} m"
        self.reset_text_var.set(label)
        self.reset_menu.entryconfigure(0, label=label)

    def _set_current_as_default(self) -> None:
        h = self._clamp_absolute_height(self._current_height())
        ok, error = self._save_default_height(h)
        if not ok:
            self._set_status(f"Default not saved: {error}")
            return
        self.default_height = h
        self._update_reset_menu()
        self._set_status(f"Default saved: {h:.2f} m")

    def _current_height(self) -> float:
        if self.cur_height is not None:
            return self.cur_height
        return self._slider_position_to_height(float(self.slider_var.get()))

    def _apply_preset(self, h: float) -> None:
        h = self._clamp_absolute_height(h)
        self._set_display_height(h)
        self._send_height(h)

    def _apply_scale(self, factor: float) -> None:
        self._apply_preset(self._current_height() * factor)

    @staticmethod
    def _clamp_absolute_height(h: float) -> float:
        return max(VRCHAT_ABSOLUTE_MIN, min(VRCHAT_ABSOLUTE_MAX, h))

    @staticmethod
    def _log_lerp(start: float, end: float, t: float) -> float:
        return math.exp(math.log(start) + (math.log(end) - math.log(start)) * t)

    @staticmethod
    def _log_unlerp(start: float, end: float, value: float) -> float:
        return (math.log(value) - math.log(start)) / (math.log(end) - math.log(start))

    @classmethod
    def _slider_position_to_height(cls, position: float) -> float:
        position = max(SLIDER_MIN, min(SLIDER_MAX, position))
        low_end = SLIDER_LOW_FRACTION
        recommended_end = SLIDER_LOW_FRACTION + SLIDER_RECOMMENDED_FRACTION

        if position <= low_end:
            return cls._log_lerp(
                VRCHAT_ABSOLUTE_MIN,
                SAFE_MIN,
                position / SLIDER_LOW_FRACTION,
            )
        if position <= recommended_end:
            return cls._log_lerp(
                SAFE_MIN,
                SAFE_MAX,
                (position - low_end) / SLIDER_RECOMMENDED_FRACTION,
            )
        return cls._log_lerp(
            SAFE_MAX,
            VRCHAT_ABSOLUTE_MAX,
            (position - recommended_end) / SLIDER_HIGH_FRACTION,
        )

    @classmethod
    def _height_to_slider_position(cls, h: float) -> float:
        h = cls._clamp_absolute_height(h)
        recommended_end = SLIDER_LOW_FRACTION + SLIDER_RECOMMENDED_FRACTION

        if h <= SAFE_MIN:
            return SLIDER_LOW_FRACTION * cls._log_unlerp(
                VRCHAT_ABSOLUTE_MIN, SAFE_MIN, h
            )
        if h <= SAFE_MAX:
            return SLIDER_LOW_FRACTION + SLIDER_RECOMMENDED_FRACTION * cls._log_unlerp(
                SAFE_MIN, SAFE_MAX, h
            )
        return recommended_end + SLIDER_HIGH_FRACTION * cls._log_unlerp(
            SAFE_MAX, VRCHAT_ABSOLUTE_MAX, h
        )

    def _set_display_height(self, h: float) -> None:
        # Programmatic slider moves should update the UI without echoing to VRChat.
        self._suppress_send = True
        try:
            self.slider_var.set(self._height_to_slider_position(h))
            self.height_label.configure(text=f"{h:.2f} m")
        finally:
            self._suppress_send = False

    def _has_custom_world_limits(self) -> bool:
        if self.cur_min is None or self.cur_max is None:
            return True
        return not (
            math.isclose(self.cur_min, WORLD_DEFAULT_MIN)
            and math.isclose(self.cur_max, WORLD_DEFAULT_MAX)
        )

    def _send_height(self, h: float) -> None:
        try:
            self.client.send_message(ADDR_HEIGHT, float(h))
        except Exception as exc:
            # OSC send errors originate in a UI callback; surface them instead
            # of letting a backend/socket edge case crash the Tk event loop.
            self.warning_var.set(f"Send failed: {exc}")
            self.warning.pack(fill="x", padx=12, pady=(0, 6))
            return
        self._refresh_warning(pending=h)

    # --- warnings ---------------------------------------------------------

    def _refresh_warning(self, pending: float | None = None) -> None:
        h = pending if pending is not None else self.cur_height
        msgs: list[str] = []

        if self.cur_allowed is False:
            msgs.append(
                "Scaling is disabled by the current world or Udon — writes to /avatar/eyeheight will be ignored."
            )

        if h is not None and not (SAFE_MIN <= h <= SAFE_MAX):
            msgs.append(
                f"Height {h:.2f} m is outside VRChat's officially supported {SAFE_MIN}–{SAFE_MAX} m range."
            )

        has_custom_world_limits = self._has_custom_world_limits()

        if h is not None and has_custom_world_limits and self.cur_min is not None and h < self.cur_min:
            msgs.append(
                f"Below this world's Udon-configured minimum ({self.cur_min:.2f} m); the world may snap you back."
            )
        if h is not None and has_custom_world_limits and self.cur_max is not None and h > self.cur_max:
            msgs.append(
                f"Above this world's Udon-configured maximum ({self.cur_max:.2f} m); the world may snap you back."
            )

        if msgs:
            self.warning_var.set("⚠  " + "\n⚠  ".join(msgs))
            self.warning.pack(fill="x", padx=12, pady=(0, 6))
        else:
            self.warning_var.set("")
            self.warning.pack_forget()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

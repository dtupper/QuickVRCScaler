"""QuickVRCScaler — small OSC client for VRChat avatar scaling.

Sends to 127.0.0.1:9000 (VRChat input) and listens on 127.0.0.1:9001
(VRChat output). Endpoints per https://docs.vrchat.com/docs/osc-avatar-scaling
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from pythonosc import udp_client
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

try:
    from tinyoscquery.queryservice import OSCQueryService
    from tinyoscquery.query import OSCQueryBrowser, OSCQueryClient
    from tinyoscquery.utility import get_open_tcp_port, get_open_udp_port
    _OSCQUERY_AVAILABLE = True
except Exception:
    _OSCQUERY_AVAILABLE = False

VRCHAT_HOST = "127.0.0.1"
SEND_PORT = 9000
LISTEN_PORT = 9001

ADDR_HEIGHT = "/avatar/eyeheight"
ADDR_MIN = "/avatar/eyeheightmin"
ADDR_MAX = "/avatar/eyeheightmax"
ADDR_ALLOWED = "/avatar/eyeheightscalingallowed"

SAFE_MIN = 0.1
SAFE_MAX = 100.0
SLIDER_MIN = 0.1
SLIDER_MAX = 5.0


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("QuickVRCScaler")
        root.geometry("540x640")
        root.minsize(540, 640)

        self.client = udp_client.SimpleUDPClient(VRCHAT_HOST, SEND_PORT)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()

        # State reported by VRChat
        self.cur_height: float | None = None
        self.cur_min: float | None = None
        self.cur_max: float | None = None
        self.cur_allowed: bool | None = None

        # Suppress send when slider is updated programmatically from incoming OSC
        self._suppress_send = False

        self._build_ui()
        self._start_server()
        self._start_oscquery()
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

        self.slider_var = tk.DoubleVar(value=1.6)
        self.slider = ttk.Scale(
            slider_frame,
            from_=SLIDER_MIN,
            to=SLIDER_MAX,
            orient="horizontal",
            variable=self.slider_var,
            command=self._on_slider,
        )
        self.slider.pack(side="left", fill="x", expand=True)

        self.height_label = ttk.Label(
            slider_frame, text="1.60 m", width=8, anchor="e", font=("Segoe UI", 11)
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
        ttk.Button(entry_frame, text="Reset 1.6 m", command=self._reset).pack(
            side="right"
        )
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
        self.info_min_var = tk.StringVar(value="Udon min:    —")
        self.info_max_var = tk.StringVar(value="Udon max:    —")
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

        status = ttk.Label(
            self.root,
            text=f"Send → {VRCHAT_HOST}:{SEND_PORT}    Listen ← :{LISTEN_PORT}",
            foreground="#666",
        )
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
            ("0.1 m", 0.1), ("1 m", 1.0), ("2 m", 2.0), ("3 m", 3.0),
            ("5 m", 5.0), ("10 m", 10.0), ("25 m", 25.0), ("Reset 1.6 m", 1.6),
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
        except Exception as exc:
            self.oscquery = None
            print(f"[QuickVRCScaler] OSCQuery unavailable: {exc}")

    def _poll_oscquery(self) -> None:
        """Discover VRChat's OSCQuery service and pull current values."""
        if _OSCQUERY_AVAILABLE:
            threading.Thread(target=self._poll_oscquery_worker, daemon=True).start()
        # Schedule next poll regardless (cheap if no service found).
        self.root.after(10_000, self._poll_oscquery)

    def _poll_oscquery_worker(self) -> None:
        try:
            browser = OSCQueryBrowser()
            # Give zeroconf a moment to discover services.
            import time as _t
            _t.sleep(0.8)
            # Restrict to services that expose VRChat's eye-height endpoint.
            # This avoids latching onto unrelated OSCQuery apps on the same
            # machine (e.g. VRCFT, which exposes its own /avatar/parameters).
            candidates = browser.find_nodes_by_endpoint_address(ADDR_HEIGHT)
        except Exception as exc:
            print(f"[QuickVRCScaler] OSCQuery browse failed: {exc}")
            return

        svc = self._pick_vrchat_service(candidates)
        if svc is None:
            return

        try:
            client = OSCQueryClient(svc)
            for addr, key in (
                (ADDR_HEIGHT, "height"),
                (ADDR_MIN, "min"),
                (ADDR_MAX, "max"),
                (ADDR_ALLOWED, "allowed"),
            ):
                try:
                    node = client.query_node(addr)
                except Exception:
                    node = None
                if node is None:
                    continue
                value = getattr(node, "value", None)
                if isinstance(value, (list, tuple)) and value:
                    value = value[0]
                if value is None:
                    continue
                self.events.put((key, value))
        except Exception:
            return

    @staticmethod
    def _pick_vrchat_service(candidates):
        """Choose the best OSCQuery service from `find_nodes_by_endpoint_address`.

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

    def _handle_osc(self, _addr: str, key: str, *args) -> None:
        if not args:
            return
        # OSC handler runs on server thread; hand off to UI thread.
        self.events.put((key, args[0]))

    def _drain_events(self) -> None:
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
            # Move slider without echoing back to VRChat.
            self._suppress_send = True
            try:
                clamped = max(SLIDER_MIN, min(SLIDER_MAX, h))
                self.slider_var.set(clamped)
                self.height_label.configure(text=f"{h:.2f} m")
            finally:
                self._suppress_send = False
        elif key == "min":
            try:
                self.cur_min = float(value)  # type: ignore[arg-type]
                self.info_min_var.set(f"Udon min:    {self.cur_min:.3f} m")
            except (TypeError, ValueError):
                return
        elif key == "max":
            try:
                self.cur_max = float(value)  # type: ignore[arg-type]
                self.info_max_var.set(f"Udon max:    {self.cur_max:.3f} m")
            except (TypeError, ValueError):
                return
        elif key == "allowed":
            self.cur_allowed = bool(value)
            self.info_allowed_var.set(
                f"Scaling:     {'allowed' if self.cur_allowed else 'BLOCKED by world/Udon'}"
            )
        self._refresh_warning()

    # --- UI actions -------------------------------------------------------

    def _on_slider(self, _value: str) -> None:
        h = float(self.slider_var.get())
        self.height_label.configure(text=f"{h:.2f} m")
        if self._suppress_send:
            return
        self._send_height(h)

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
        # Hard clamp to VRChat's absolute supported range.
        h = max(0.01, min(10000.0, h))
        self._suppress_send = True
        try:
            self.slider_var.set(max(SLIDER_MIN, min(SLIDER_MAX, h)))
            self.height_label.configure(text=f"{h:.2f} m")
        finally:
            self._suppress_send = False
        self._send_height(h)

    def _reset(self) -> None:
        self._apply_preset(1.6)

    def _current_height(self) -> float:
        if self.cur_height is not None:
            return self.cur_height
        return float(self.slider_var.get())

    def _apply_preset(self, h: float) -> None:
        h = max(0.01, min(10000.0, h))
        self._suppress_send = True
        try:
            self.slider_var.set(max(SLIDER_MIN, min(SLIDER_MAX, h)))
            self.height_label.configure(text=f"{h:.2f} m")
        finally:
            self._suppress_send = False
        self._send_height(h)

    def _apply_scale(self, factor: float) -> None:
        self._apply_preset(self._current_height() * factor)

    def _send_height(self, h: float) -> None:
        try:
            self.client.send_message(ADDR_HEIGHT, float(h))
        except OSError as exc:
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

        if h is not None and self.cur_min is not None and h < self.cur_min:
            msgs.append(
                f"Below this avatar's Udon-configured minimum ({self.cur_min:.2f} m); the avatar may snap back."
            )
        if h is not None and self.cur_max is not None and h > self.cur_max:
            msgs.append(
                f"Above this avatar's Udon-configured maximum ({self.cur_max:.2f} m); the avatar may snap back."
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

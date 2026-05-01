# AGENTS.md

Working notes for AI coding agents on this repo. Optimised for fast onboarding — read this first, then `quickvrcscaler.py`.

## What this is

A small Tk desktop app that wraps VRChat's [avatar scaling OSC endpoints](https://docs.vrchat.com/docs/osc-avatar-scaling). One Python file (`quickvrcscaler.py`), one test file (`tests/test_app.py`), packaged into a single-file Windows EXE via PyInstaller. End users are streamers driving it from a VR overlay with shaky controllers — the UI is tuned for chunky hit-targets, not density.

Tech stack:
- Python 3.10+ (CI builds against 3.12), `from __future__ import annotations` everywhere
- `tkinter` / `ttk` for UI (no other UI deps)
- `python-osc` for UDP send/receive
- `tinyoscquery` (pulled from GitHub) for mDNS-based service discovery so VRChat pushes current state on connect
- `requests` (transitive via tinyoscquery) for direct OSCQuery HTTP — see "Battle scars" below for why we use it directly

## Running and developing

A `.\venv` is checked-out-locally pattern; create it once, then everything else is scripted.

```pwsh
# One-time setup
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt

# Run the app
.\venv\Scripts\python quickvrcscaler.py

# Run all unit tests (matches CI; -W error::ResourceWarning catches socket leaks)
.\venv\Scripts\python -W error::ResourceWarning -m unittest discover -s tests -v

# Local release-style build (mirrors .github/workflows/build.yml exactly)
.\build.ps1            # runs tests then PyInstaller
.\build.ps1 -SkipTests # skip tests for a faster iteration
```

`build.ps1` produces `dist/QuickVRCScaler.exe` (~14.7 MB) and is the same command CI runs. If a change works locally via `.\build.ps1`, it'll work in CI.

## Code structure

```
quickvrcscaler.py    # single-file app: App class + module-level main()
tests/test_app.py    # unittest suite, all 46 tests hermetic (no sockets, no zeroconf)
build.ps1            # local build script mirroring CI
.github/workflows/   # build.yml: test on every push/PR, build+release on v* tags
requirements.txt     # python-osc, tinyoscquery (from git)
```

`quickvrcscaler.py` is intentionally one file — it's small enough that splitting it adds more friction than it removes. Resist the urge to split it unless something specific pushes it past ~700 lines.

## Conventions

- **Type hints with `from __future__ import annotations`.** Use modern syntax (`str | None`, `list[Foo]`).
- **Comments explain *why*, not *what*.** The code already says what it does; comments earn their place by explaining trade-offs, history, or non-obvious constraints. Multi-line context goes above the block; one-liners go to the right.
- **Docstrings only where they add value.** Methods like `_apply_event` don't need one. `_pick_vrchat_service` does, because the contract is non-obvious.
- **Errors swallowed only with a reason.** `except Exception:` is fine for "best-effort cleanup on shutdown" or "OSCQuery failure shouldn't crash the UI", but write a `# why` comment next to it. Never bury a real error.
- **No emoji in code or docs unless the user explicitly asks.** The UI uses a `⚠` for warnings — that one's intentional.
- **Tk threading rule:** all Tk mutations happen on the main thread. Background threads (OSC server, OSCQuery polling) push events to `self.events: queue.Queue` and the main thread drains via `_drain_events` every 50 ms. **Never call `self.foo_var.set(...)` from a background thread.**

## Tests

`tests/test_app.py` patches out `_start_server`, `_start_oscquery`, `_poll_oscquery`, and `udp_client.SimpleUDPClient` in `_BaseAppTest.setUpClass` so tests don't bind sockets or touch zeroconf. The originals are saved on the test class as `_orig_start_server` / `_orig_start_query` / `_orig_poll_query` if a specific test needs to exercise the real method (see `OSCQueryPollLifecycleTests` for the pattern).

When adding a feature, add a test. When fixing a bug, add a regression test that pins the bug closed — `test_browser_is_created_only_once` is the canonical example.

Tests must stay hermetic: no real sockets, no real HTTP, no real zeroconf. Use `unittest.mock.patch` / `MagicMock`. CI runs with `-W error::ResourceWarning`, so a leaked socket fails the build.

## Battle scars (read before touching OSCQuery code)

The app polls VRChat's OSCQuery service every 10 s to keep readouts fresh. Two real bugs shipped here, both fixed in v0.1.2 — don't reintroduce them:

1. **`OSCQueryBrowser` is long-lived (`self._browser`), created once on first poll and reused.** Recreating it per poll leaks `Zeroconf()` instances (no `__del__` on `OSCQueryBrowser`, internal threads keep themselves alive). At 10 s intervals, that's ~720 leaked instances after 2 hours, each running mDNS queries on `224.0.0.251:5353`, eventually starving the GIL until Tk can't process clicks. The pinned regression test is `test_browser_is_created_only_once`.

2. **Never call `tinyoscquery.OSCQueryClient.query_node` / `get_host_info` directly.** They use `requests.get(url)` with no timeout, so a half-dead peer hangs the worker thread forever. Use `App._http_get_json(url)` instead — it enforces `OSCQUERY_HTTP_TIMEOUT` (2 s). Pinned by `test_http_get_json_always_passes_timeout`.

Other things to know:
- `_poll_in_flight` flag prevents a stalled worker from causing follow-up workers to pile up. Set in `_poll_oscquery` *before* spawning, cleared in the worker's `finally`.
- `_pick_vrchat_service` exists because VRCFT (face-tracking app) and other peers also expose `/avatar/...` over OSCQuery on the same machine. Never just take the first discovered service.
- `_suppress_send` flag on the `App` instance: set when the slider is being moved *programmatically* (in response to incoming OSC) so we don't echo VRChat's own value back to it and create a feedback loop. Set/clear in a `try/finally` around the `slider_var.set()` call.

## VRChat OSC specifics

- Sends to `127.0.0.1:9000` (VRChat input), listens on `127.0.0.1:9001` (VRChat output).
- VRChat only emits `/avatar/eyeheight*` on *change* events. We register an OSCQuery service so VRChat mirrors current values to us on connect — without that, readouts stay blank until the user changes avatars.
- The `/avatar/eyeheight*` write path is silently ignored when `eyeheightscalingallowed` is False (world/Udon disabled scaling). Surface that in the warning banner; don't silently fail.

## CI and releases

- `.github/workflows/build.yml` runs tests on every push/PR. The build job is gated on `startsWith(github.ref, 'refs/tags/v')` and runs only for `v*` tags.
- The build job has `permissions: contents: write` (required for `softprops/action-gh-release@v2` to attach the EXE).
- Release flow: merge to `main` with `--no-ff`, then `git tag -a vX.Y.Z -m "..."`, then `git push origin main && git push origin vX.Y.Z`. CI builds the EXE and attaches it to a generated release.
- Use semver. Patch (`v0.1.x`) for fixes, minor for features.

## Don'ts

- **Don't `git tag -f` or force-push tags.** Bump the version instead. Tag history is part of the release record.
- **Don't `--amend` after a hook fails.** The commit didn't happen — re-stage and make a new commit.
- **Don't add new top-level Python files for "utilities".** Keep helpers as static/class methods on `App` or module-level functions in `quickvrcscaler.py` until there's a real reason to split.
- **Don't add a dependency without checking it bundles cleanly with PyInstaller.** Run `.\build.ps1` to confirm.
- **Don't run the app in a test.** Tk's `mainloop()` blocks. Tests construct `App(root)` with `root.withdraw()` and never call `mainloop`.

## When in doubt

Read `quickvrcscaler.py` end-to-end (~570 lines) before making non-trivial changes. The whole app fits in your head; it's worth the read.

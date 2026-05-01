# QuickVRCScaler

A small desktop GUI for VRChat's [avatar scaling OSC endpoints](https://docs.vrchat.com/docs/osc-avatar-scaling). Drag a slider, see your eye height update live, and get warnings when you ask for something the world or avatar won't allow.

## Features

- **Slider** for eye height (0.1 m – 5.0 m) plus an exact-value entry that accepts VRChat's full 0.01 m – 10 000 m range.
- **Live readouts** of every scaling endpoint VRChat exposes:
  - `/avatar/eyeheight`
  - `/avatar/eyeheightmin`
  - `/avatar/eyeheightmax`
  - `/avatar/eyeheightscalingallowed`
- **Warnings** when:
  - the world or Udon has disabled scaling (writes will be ignored),
  - the requested height is outside VRChat's officially supported 0.1 m – 100 m range,
  - the requested height is outside the avatar's Udon-configured min/max.
- **OSCQuery integration** so VRChat actively pushes current values on connect — readouts populate without waiting for an avatar change.

## Requirements

- Windows (the prebuilt EXE is Windows-only; the Python source runs anywhere Tk runs)
- Python 3.10+ if running from source
- VRChat with OSC enabled — Action Menu → Options → OSC → Enabled

## Install

### Prebuilt EXE

Grab `QuickVRCScaler.exe` from the latest [Release](../../releases) and double-click it. No install required.

### From source

```sh
git clone https://github.com/dtupper/QuickVRCScaler
cd QuickVRCScaler
pip install -r requirements.txt
python quickvrcscaler.py
```

`tinyoscquery` is pulled directly from GitHub, so `git` must be on your PATH at install time.

## Usage

1. Launch VRChat with OSC enabled.
2. Launch QuickVRCScaler.
3. Drag the slider, type a value, or hit **Reset 1.6 m**. The avatar's eye height updates live.
4. **Refresh** forces a re-poll of the read-only endpoints (min/max/allowed) via OSCQuery.

The app sends to `127.0.0.1:9000` and listens on `127.0.0.1:9001` — VRChat's standard OSC ports.

## Develop

Run the tests:

```sh
python -m unittest discover -s tests -v
```

Build a single-file EXE locally (matches what CI produces):

```sh
pip install pyinstaller
pyinstaller --onefile --windowed --name QuickVRCScaler \
  --collect-all zeroconf --collect-all tinyoscquery \
  quickvrcscaler.py
```

The result lands in `dist/QuickVRCScaler.exe`.

## Releases

Pushing a `v*` tag (e.g. `git tag v0.1.0 && git push --tags`) triggers CI to run tests, build the EXE, and attach it to a GitHub Release.

## License

MIT

# Gideon Visualizer

The visualizer is Gideon's browser HUD: a set of WebGL faces that react to voice state and audio levels. It can run as part of the Gideon daemon or on its own with the small Python server in this directory.

## Faces

The gallery discovers face directories that contain an `index.html` file. The included faces are:

- `board` — circuit-board layout
- `neural` — node-and-link neural display
- `radial` — radial audio display
- `rain` — falling-glyph display

## Run it standalone

The standalone server uses Python's standard library; a modern browser with WebGL support is the other requirement.

```bash
cd ai-visualizer
./run.sh
```

The server listens on `127.0.0.1:8790` and opens the configured face. To preview without a running voice daemon, use mock state:

```bash
./run.sh --mock speaking
```

Available mock states are `idle`, `listening`, `thinking`, and `speaking`. To keep the server in the terminal without opening a browser, use `./run.sh --no-open`. Choose another port with `./run.sh --port 8791 --no-open`. Ctrl-C stops the server.

Running the standalone server while `gideon.py` already owns port 8790 will fail with a port-in-use message. Use a different port for the standalone preview, or stop the daemon first.

## Connect a signal bus

By default, the standalone server reads `.voice_*` files from this directory. Set `bus_dir` in `ai-visualizer.json` to the directory containing the files to connect another producer. For Gideon's daemon files, for example:

```json
{
  "bus_dir": "../signals",
  "port": 8791
}
```

This example assumes `ai-visualizer.json` is saved in this directory. The daemon itself serves the HUD from its embedded HTTP server and does not need a second visualizer process. The file formats and local HTTP endpoints are documented in [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

The standalone server exposes `GET /state` and `GET /config`. Faces poll `/state` for the current animation state. The gallery reads `/config` for the display name, optional badge, selected face, thinking-sound setting, and discovered faces. Thinking audio plays from `assets/thinking.wav` when enabled, unless the bus reports that another thinking sound is already playing.

## Add a face

Create a directory under `faces/` with an `index.html`. The server discovers it for the gallery. An optional `face.json` can provide face metadata such as `title` and `tagline`; the directory name remains its face ID. The existing faces share rendering and state helpers from `core.js`.

## Files

- `server.py` — standalone loopback HTTP server and file-bus reader.
- `core.js` — shared browser polling, state easing, demo mode, and HUD helpers.
- `faces/` — included face pages and their rendering code.
- `assets/` — local visual and audio assets.

The visualizer is licensed under AGPL-3.0-or-later and is adapted from ai-visualizer by Jared Rhodenizer. See [`LICENSE`](LICENSE) for the license text and [`NOTICE`](NOTICE) for attribution and the list of modifications.

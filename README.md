# layout-driver

Drives a 6-screen LED wall from a single composited browser page. `config/screens.yaml`
is the source of truth for the wall's geometry: the server hands that layout to the page,
the page draws every screen into one 3840×2160 canvas, and the NDI broadcaster captures
that page in a kiosk Chrome instance and sends it out as a single NDI stream (plus audio).
Apps are plain static directories that import `/layout-driver.js`; the framework itself
never knows what they draw.

## Setup

```bash
uv sync                        # base framework
uv sync --extra flux-gallery   # also the flux-gallery app (torch, diffusers -- ~2GB+)
uv run playwright install chromium   # needed by the NDI broadcaster and noraebang's smoke test
```

Only one app can be active at a time — `APP_DIR` selects it, and the apps are not designed
to run simultaneously.

## Running

Framework alone (serves `apps/test-pattern/static/`, the default):

```bash
./run.sh
```

`run.sh` also accepts the app directory as a positional argument, equivalent to setting
`APP_DIR`: `./run.sh /path/to/app/static`.

If you preview the composited page directly in a browser (rather than only through the
NDI broadcaster, which sets `ignore_https_errors`), you'll hit a self-signed-certificate
warning on first load — click through it, that's expected (see the framework spec §3.1).

Once running, confirm the NDI stream is actually visible with an NDI monitoring tool on
the same network (e.g. NDI Tools' Studio Monitor) — this repo's own job stops at
producing the stream; what downstream AV hardware does with it is out of scope here.

### noraebang-generative

Pure client-side p5.js generative sketches, one per screen, with a looping audio bed.

```bash
APP_DIR=$(pwd)/apps/noraebang-generative/static ./run.sh
```

### flux-gallery

Two processes. The first is the framework plus broadcaster, serving flux-gallery's page
(which enables the framework's image mode and screenshot responder):

```bash
APP_DIR=$(pwd)/apps/flux-gallery/static ./run.sh
```

The second is the worker, which expands prompts with Gemini, generates images with FLUX,
and pushes them to screens over HTTP:

```bash
GEMINI_API_KEY=... apps/flux-gallery/run.sh
```

- `GEMINI_API_KEY` — **required** by the worker (prompt expansion).
- `HF_TOKEN` — optional; only needed for gated or private models. FLUX.1-schnell, the
  default, is open.

Disk usage: the worker retains the most recent 200 images per screen plus 200 full-wall
3840×2160 screenshots, roughly 2–3GB steady-state, under `apps/flux-gallery/output/`
(gitignored).

## Testing

```bash
uv run pytest -v
node --test static/*.test.mjs apps/noraebang-generative/static/*.test.mjs
```

flux-gallery's `test_worker.py` and `test_gemini_expander.py` skip themselves unless the
`flux-gallery` extra is installed (`uv sync --extra flux-gallery`); noraebang's smoke test
needs `playwright install chromium`.

## Regenerating the placeholder audio track

`apps/noraebang-generative/static/assets/track.mp3` is a synthesized placeholder, not
licensed music (see the `NOTICE.md` beside it). To regenerate:

```bash
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "sine=frequency=110:duration=30" \
  -f lavfi -i "sine=frequency=164.81:duration=30" \
  -f lavfi -i "sine=frequency=220:duration=30" \
  -filter_complex "[0:a]volume=0.25[a0];[1:a]volume=0.2[a1];[2:a]volume=0.15[a2];[a0][a1][a2]amix=inputs=3:duration=longest[mixed];[mixed]afade=t=in:st=0:d=2,afade=t=out:st=28:d=2[out]" \
  -map "[out]" -ac 2 -ar 44100 -b:a 128k apps/noraebang-generative/static/assets/track.mp3
```

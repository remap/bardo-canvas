# layout-driver

Drives a 6-screen LED wall from a single composited browser page. `config/screens.yaml`
is the source of truth for the wall's geometry: the server hands that layout to the page,
the page draws every screen into one 3840×2160 canvas, and the NDI broadcaster captures
that page in a headless Chrome instance and sends it out as a single NDI stream (plus audio).
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

### Prototyping an app

The NDI broadcaster runs a fully headless, uninstrumented browser — there is no window to
look at, on purpose (see "Why the broadcaster is headless" below). To actually watch an
app while you build it, in a normal resizable window, just open the composited page
directly in any ordinary browser:

```
https://localhost:8443/
```

(click through the self-signed-cert warning above). This is not a special dev mode — it's
the same page the broadcaster captures, driven by the same WebSocket sync the broadcaster
uses, so it shows the real layout and behavior. `buildRoot()` already rescales the whole
composited canvas to fit the window on every resize, so the window is freely resizable
with no extra code or flags.

This is safe to leave open alongside a running broadcaster: nothing about the broadcaster
ever touches, screenshots, or otherwise depends on this tab, so resizing or closing it has
zero effect on the NDI feed. Two things won't match exactly between a preview tab and the
broadcast, both expected: audio will double up if both are un-muted, and for direct-render
apps (noraebang-generative) each browser instance runs its own independent sketch
instance, so the two won't be pixel-identical — same layout and behavior, different random
seed/timing. For a byte-for-byte check of the actual broadcast signal, use an NDI monitor
instead (see above); reach for that only for final verification, not the everyday dev loop.

#### Why the broadcaster is headless

Earlier versions launched a real, visible kiosk-mode Chrome window and captured it. That
window doesn't exist anymore, and needing this section is exactly why: capturing a
*visible* window at 30fps via repeated CDP screenshot calls was found to visibly flash the
window itself (confirmed by isolating the same launch with the capture loop removed
entirely — no flashing without it). Headless has no on-screen presentation to disrupt.
Nothing here needs a real window in the first place — app audio is a plain `<audio>`
element, not DRM.

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

**Known limitation:** NDI capture fps degrades steadily while this worker is running
(not while idle), down to single digits over several minutes, with no recovery on its
own short of restarting the broadcaster. This is specific to flux-gallery — traced via
native profiling to the closed-source NDI SDK itself, not to Flux/GPU contention or
anything else in this repo. See framework spec §3.4a for the full investigation.

## Performance and correctness: how NDI capture actually works

The broadcaster does **not** use any Chrome DevTools Protocol screenshot API
(`Page.captureScreenshot`, `Page.startScreencast`) to read the wall's content — every one
of those was tried and independently found, via live testing, to unreliably return solid
black for this app's canvases despite them holding correct, verified pixel data. This
matches a known class of Chromium bug (canvas content valid and readable from the page's
own JS not reliably reaching Chromium's viewport-level capture pipeline). Instead,
`static/layout-driver.js` exposes `window.__ndiCaptureDataURL()`, which composites the
wall with plain `ctx.drawImage()`/`canvas.toDataURL()` — the same reliable, in-page
approach `/api/screenshot` already used — and the broadcaster calls it directly via
Playwright's `page.evaluate()`, over the same CDP connection already driving the browser.
No HTTP server, no network round trip, no second browser tab. See framework spec §3.4 for
the full history of what was tried and why each attempt was abandoned.

On macOS, `_chrome_launch_args()` (`ndi_broadcaster/launcher.py`) adds `--use-angle=metal`:
Playwright's bundled headless Chromium otherwise defaults to the SwiftShader *software*
renderer (confirmed live via CDP's `SystemInfo.getInfo`), rasterizing every canvas draw
and every wall capture entirely on the CPU. This flag is macOS-only (ANGLE's Metal backend
doesn't exist elsewhere); other platforms get Chromium's own default backend selection.

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

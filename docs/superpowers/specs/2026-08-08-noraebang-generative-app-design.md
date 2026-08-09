# Noraebang Generative Wall App — Design Spec

Status: approved for implementation planning
Scope: `apps/noraebang-generative/` — a validation app built on the layout-driver framework (see `2026-08-08-layout-driver-framework-design.md`). Self-contained under its own app directory; touches nothing in `layout_server/`, `ndi_broadcaster/`, or `static/`.

## 1. Purpose

Prove out the framework's *direct-render* path (as opposed to the Flux app's push-image path): six independent p5.js sketches, one per screen, each owning its own canvas and draw loop via `driver.getScreenContainer(id)` (where `driver` is the object returned by `initLayoutDriver()`). Dark, neon, noraebang (Korean karaoke room) aesthetic — purely generative, not audio-reactive, plus a looped ambient audio track exercising the framework's audio-output path.

## 2. Architecture

Pure static app — **no backend process**. `layout_server` serves `apps/noraebang-generative/static/` as the active `APP_DIR`. Everything happens client-side in the browser:

- `static/index.html` — loads `layout-driver.js`, `theme.js`, and the 6 sketch modules as native ES modules (no bundler).
- `static/theme.js` — shared palette module: near-black background (`#050208`-ish) and a small fixed set of neon accent colors (magenta, cyan, gold, violet, electric blue). Every sketch imports this rather than defining its own colors, so six different techniques still read as one coherent wall.
- `static/sketches/*.js` — one module per screen, each exporting a p5 instance-mode sketch factory `(p) => { p.setup = ...; p.draw = ...; }`.
- `static/config/noraebang.json` — maps screen id → sketch module name (+ optional per-sketch param overrides), so reassigning/tuning sketches never touches code. JSON rather than YAML: this file is fetched and parsed entirely in the browser, which has no built-in YAML parser (every other YAML config in this repo is consumed by Python, which already depends on PyYAML) — see the framework's global constraints for the full reasoning. It lives under `static/` rather than as a sibling of it because `layout_server`'s catch-all static mount only serves `app_static_dir` (`apps/<name>/static/`), so anything outside `static/` is unreachable to the browser.
- `assets/track.mp3` — looped ambient audio (placeholder shipped initially; see §5).

## 3. Sketch roster

| Screen | Sketch module | Concept |
|---|---|---|
| F | `flow-field.js` | Noise-driven flow-field streamlines |
| B | `particle-swarm.js` | Boids-like attractor swarm |
| C | `laser-grid.js` | Sweeping animated laser/grid lines |
| D | `mirror-ball.js` | Rotating disco-ball sparkle/glint particles |
| A | `vu-bars.js` | Non-reactive but rhythmic animated equalizer bars |
| E | `scanline-crt.js` | Retro CRT scanline + VHS glitch texture |

Each is a genuinely distinct generative technique (not the same algorithm reparameterized), unified only through the shared `theme.js` palette. `static/config/noraebang.json`:

```json
{
  "screens": [
    { "id": "F", "sketch": "flow-field" },
    { "id": "B", "sketch": "particle-swarm" },
    { "id": "C", "sketch": "laser-grid" },
    { "id": "D", "sketch": "mirror-ball" },
    { "id": "A", "sketch": "vu-bars" },
    { "id": "E", "sketch": "scanline-crt" }
  ]
}
```

## 4. Wiring to the framework

On load, `index.html`'s bootstrap script:

1. `import`s `initLayoutDriver` and `routeAudioElement` from `/layout-driver.js`, and `await`s `const driver = await initLayoutDriver();` (screens loaded from `/api/screens` as part of that call — there is no separate `.ready` to await, `initLayoutDriver()` itself resolves once the driver is usable).
2. Fetches `config/noraebang.json`, and for each screen: dynamically `import()`s the assigned sketch module, gets that screen's container via `driver.getScreenContainer(id)`, and constructs `new p5(sketchFactory, container.element)`.
3. Each p5 instance calls `p.createCanvas(container.width, container.height)` inside its own `setup()` — sized exactly to its screen, no cropping needed since these are native-resolution sketches, not pushed images.

This app never touches `enableImageMode(driver)`, `/ws`, or the push API — the layout-server and WebSocket relay are irrelevant to it, as noted in the framework spec's data-flow examples.

## 5. Audio

A single `<audio loop>` element, `src="assets/track.mp3"`, created on page load and passed to `routeAudioElement(el)` (a standalone function imported alongside `initLayoutDriver`, not a method on the driver object — routes the element's output to the configured loopback device per the framework's `config/audio.yaml`, so it reaches the NDI broadcast).

`assets/track.mp3` ships as a placeholder — a simple generated ambient loop, not a licensed music track, since sourcing real copyrighted audio isn't something to do without the user supplying it. The file path is fixed but swappable; dropping in a real track later requires no code change.

## 6. Robustness

Each sketch instantiation (step 2 in §4) is wrapped in a try/catch: a sketch module that fails to load or throws during `setup()`/`draw()` leaves that one screen's container empty/dark rather than breaking the whole page or the other 5 screens.

## 7. Performance budget

All 6 p5 instances run concurrently in a single Chrome tab feeding the NDI broadcaster at 30fps — this is the binding constraint each sketch's implementation must respect: keep particle counts and per-frame work modest, avoid full-resolution per-pixel canvas operations (the largest canvas, screen F, is 1800×1400), and prefer vectorized/batched drawing calls over per-element state updates where p5 makes that possible. This is a constraint to design against, not something to benchmark in advance of writing the sketches.

## 8. Repo structure

```
apps/noraebang-generative/
  static/
    index.html
    theme.js
    sketches/
      flow-field.js
      particle-swarm.js
      laser-grid.js
      mirror-ball.js
      vu-bars.js
      scanline-crt.js
    config/noraebang.json
    assets/track.mp3
```

## 9. Testing

- A small JS/Node unit test for the config → sketch-module mapping loader: all 6 configured screens resolve to one of the known sketch module names, and an unrecognized sketch name in config fails loudly at load rather than silently rendering nothing.
- A Playwright smoke test: load the page headless, assert exactly 6 `<canvas>` elements exist (one per screen container) and that no console errors were logged during a short run.
- No meaningful way to unit-test the generative visuals themselves — verified manually by watching the rendered wall.

## 10. Non-goals

- No audio reactivity (confirmed in the framework brainstorm — visuals are purely time/parameter-driven, not listening to any input).
- No user interaction/controls (mouse, keyboard, touch) — fully autonomous generative output.
- No runtime sketch switching — the screen→sketch mapping is fixed per `config/noraebang.json` for the life of the process; changing it requires a restart.

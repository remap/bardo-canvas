# Noraebang Generative App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `apps/noraebang-generative/` — a pure client-side app with six independent p5.js sketches (one per screen, distinct generative techniques unified by a shared dark/neon palette) plus a looped ambient audio track, validating the framework's direct-render (`getScreenContainer`) and audio-output (`routeAudioElement`) paths.

**Architecture:** No backend process. `layout_server` serves `apps/noraebang-generative/static/` as `APP_DIR`; everything happens in the browser. `index.html` awaits `initLayoutDriver()`, reads a small per-screen sketch-assignment config, dynamically imports each assigned sketch module, and constructs one `p5` instance per screen inside that screen's container (via `driver.getScreenContainer(id)`). Two small, clearly-scoped framework fixes are included in this plan (Tasks 4–5) — both were only discoverable by actually wiring up this app's audio, and both are already-known, previously-flagged risks in the framework, not new scope creep.

**Tech Stack:** Vanilla ES modules, no build step. p5.js v1.11.3 vendored locally (not CDN-loaded — a kiosk installation shouldn't depend on live internet at showtime). Node's built-in test runner for the one pure-logic unit test. Python + Playwright (already project dependencies) for the committed smoke test.

## Global Constraints

- This app lives under `apps/noraebang-generative/` — `static/` is the ONLY directory `layout_server` actually serves to the browser (it becomes `APP_DIR`), so every asset the browser needs to `fetch()` (config, audio track, vendored library) must live inside `static/`, not beside it. This is a deliberate, documented departure from the spec's literal repo-structure diagram (§8), which put `config/` and `assets/` as siblings of `static/` — that placement is unreachable by a pure client-side app, since `layout_server`'s catch-all static mount only serves `app_static_dir` (`apps/<name>/static/`), confirmed directly against `layout_server/app.py`.
- Per-screen sketch config is **JSON, not YAML** — another deliberate departure from the spec's literal `config/noraebang.yaml`. The browser has zero built-in YAML parsing; every other YAML config in this repo (`screens.yaml`, `audio.yaml`, `broadcaster.yaml`, `prompts.yaml`) is consumed by Python, which already depends on `PyYAML`. This is the one config consumed entirely by browser JS, so JSON (native `fetch(...).then(r => r.json())`, zero new dependencies, equally hand-editable for this trivially flat shape) is used instead of vendoring a YAML parser or hand-rolling one.
- Real, verified framework client API (read directly from the current `static/layout-driver.js`, not assumed from spec prose): `await initLayoutDriver() → {layoutConfig, getScreenContainer(id) → {element, width, height, x, y}, onMessage(handler)}`; `routeAudioElement(el)` (standalone function, not a driver method).
- Screen → sketch assignment (from the spec's §3 table): F→flow-field, B→particle-swarm, C→laser-grid, D→mirror-ball, A→vu-bars, E→scanline-crt.
- Shared palette (spec §2): near-black background (`#050208`), a small fixed set of neon accents (magenta, cyan, gold, violet, electric blue) — every sketch imports these from one `theme.js`, never defines its own colors.
- Each sketch instantiation must be individually try/caught so one broken sketch leaves only its own screen dark, not the whole page (spec §6).
- Performance budget (spec §7): 6 p5 instances run concurrently in one Chrome tab feeding 30fps NDI capture — no `loadPixels()`/`updatePixels()` per-pixel manipulation, no unbounded per-frame loops; keep each sketch's per-frame work to simple vector/shape drawing with bounded iteration counts (low hundreds at most).
- No audio reactivity, no user interaction/controls, no runtime sketch reassignment (spec §10 non-goals) — sketches only ever read `t`/noise/time, never microphone/mouse/keyboard input, and the screen→sketch mapping is fixed for the process lifetime.
- `ruff format`/`ruff check` must pass on any Python file touched (Tasks 5–6 touch Python).

---

## Task 1: Vendor p5.js, shared theme, and sketch-assignment config

**Files:**
- Create: `apps/noraebang-generative/static/vendor/p5.min.js`
- Create: `apps/noraebang-generative/static/vendor/NOTICE.md`
- Create: `apps/noraebang-generative/static/theme.js`
- Create: `apps/noraebang-generative/static/config/noraebang.json`
- Create: `apps/noraebang-generative/static/sketch-loader.js`
- Test: `apps/noraebang-generative/static/sketch-loader.test.mjs`

**Interfaces:**
- Produces: `theme.js` exports `BACKGROUND_COLOR` (string) and `PALETTE` (array of 5 hex color strings); `sketch-loader.js` exports `validateSketchAssignments(config) -> Array<{id, sketch}>` (throws on an unrecognized sketch name) and `sketchModulePath(sketchName) -> string`.

- [ ] **Step 1: Vendor p5.js at a pinned version**

```bash
mkdir -p apps/noraebang-generative/static/vendor
curl -s -o apps/noraebang-generative/static/vendor/p5.min.js https://cdn.jsdelivr.net/npm/p5@1.11.3/lib/p5.min.js
shasum -a 256 apps/noraebang-generative/static/vendor/p5.min.js
```

Expected sha256: `af51e6211e061b5ae463fbc5c3c1c272e5ca67fa560ed3513fde17325d837506` (verified against this exact URL when this plan was written — if it doesn't match, the CDN served something different than expected; stop and investigate rather than proceeding with an unverified file).

Create `apps/noraebang-generative/static/vendor/NOTICE.md`:

```markdown
# Vendored dependencies

## p5.js

- Version: 1.11.3
- Source: https://cdn.jsdelivr.net/npm/p5@1.11.3/lib/p5.min.js
- License: LGPL-2.1 (see https://github.com/processing/p5.js/blob/main/license.txt)
- sha256: af51e6211e061b5ae463fbc5c3c1c272e5ca67fa560ed3513fde17325d837506

Loaded as a plain global script (`<script src="/vendor/p5.min.js">`), not an ES
module — it attaches `p5` to `window`, which app modules reference directly.
Vendored locally rather than CDN-loaded so this app doesn't depend on live
internet access at showtime.
```

- [ ] **Step 2: Create the shared theme module**

Create `apps/noraebang-generative/static/theme.js`:

```js
export const BACKGROUND_COLOR = "#050208";

export const PALETTE = ["#ff2fd1", "#2fe1ff", "#ffd23f", "#8a2fff", "#2f7bff"];
```

- [ ] **Step 3: Create the sketch-assignment config**

```bash
mkdir -p apps/noraebang-generative/static/config
```

Create `apps/noraebang-generative/static/config/noraebang.json`:

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

- [ ] **Step 4: Write the failing test for the sketch loader**

Create `apps/noraebang-generative/static/sketch-loader.test.mjs`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { validateSketchAssignments, sketchModulePath } from "./sketch-loader.js";

test("validateSketchAssignments accepts all six real screen/sketch pairs", () => {
  const config = {
    screens: [
      { id: "F", sketch: "flow-field" },
      { id: "B", sketch: "particle-swarm" },
      { id: "C", sketch: "laser-grid" },
      { id: "D", sketch: "mirror-ball" },
      { id: "A", sketch: "vu-bars" },
      { id: "E", sketch: "scanline-crt" },
    ],
  };
  const screens = validateSketchAssignments(config);
  assert.equal(screens.length, 6);
});

test("validateSketchAssignments throws loudly on an unrecognized sketch name", () => {
  const config = { screens: [{ id: "F", sketch: "not-a-real-sketch" }] };
  assert.throws(() => validateSketchAssignments(config), /Unknown sketch/);
});

test("sketchModulePath resolves a sketch name to its module path", () => {
  assert.equal(sketchModulePath("flow-field"), "./sketches/flow-field.js");
});
```

- [ ] **Step 5: Run test to verify it fails**

Run: `node --test apps/noraebang-generative/static/sketch-loader.test.mjs`
Expected: FAIL — `Cannot find module './sketch-loader.js'`.

- [ ] **Step 6: Implement `sketch-loader.js`**

```js
const KNOWN_SKETCHES = new Set([
  "flow-field",
  "particle-swarm",
  "laser-grid",
  "mirror-ball",
  "vu-bars",
  "scanline-crt",
]);

export function validateSketchAssignments(config) {
  for (const screen of config.screens) {
    if (!KNOWN_SKETCHES.has(screen.sketch)) {
      throw new Error(`Unknown sketch "${screen.sketch}" assigned to screen "${screen.id}"`);
    }
  }
  return config.screens;
}

export function sketchModulePath(sketchName) {
  return `./sketches/${sketchName}.js`;
}
```

- [ ] **Step 7: Run test to verify it passes**

Run: `node --test apps/noraebang-generative/static/sketch-loader.test.mjs`
Expected: 3 passed.

- [ ] **Step 8: Commit**

```bash
git add apps/noraebang-generative/static/vendor/ apps/noraebang-generative/static/theme.js apps/noraebang-generative/static/config/ apps/noraebang-generative/static/sketch-loader.js apps/noraebang-generative/static/sketch-loader.test.mjs
git commit -m "feat: scaffold noraebang-generative with vendored p5.js, theme, and sketch config"
```

---

## Task 2: Six generative sketches

**Files:**
- Create: `apps/noraebang-generative/static/sketches/flow-field.js`
- Create: `apps/noraebang-generative/static/sketches/particle-swarm.js`
- Create: `apps/noraebang-generative/static/sketches/laser-grid.js`
- Create: `apps/noraebang-generative/static/sketches/mirror-ball.js`
- Create: `apps/noraebang-generative/static/sketches/vu-bars.js`
- Create: `apps/noraebang-generative/static/sketches/scanline-crt.js`

**Interfaces:**
- Consumes: `theme.js`'s `BACKGROUND_COLOR`/`PALETTE` (Task 1).
- Produces: each module exports exactly one function, `createSketch(width, height) -> (p) => void` — a factory that returns a p5 instance-mode sketch function closing over the fixed `width`/`height` for that screen. `p.createCanvas(width, height)` is called inside that returned function's `p.setup`.

No automated test is possible for generative visuals (per spec §9) — verification is a real headless-Playwright visual/console-error check in Step 2, not skipped.

- [ ] **Step 1: Implement all six sketches**

```bash
mkdir -p apps/noraebang-generative/static/sketches
```

Create `apps/noraebang-generative/static/sketches/flow-field.js` (Screen F — noise-driven flow-field streamlines):

```js
import { BACKGROUND_COLOR, PALETTE } from "../theme.js";

const PARTICLE_COUNT = 350;
const NOISE_SCALE = 0.0025;
const STEP_SIZE = 2.2;

export function createSketch(width, height) {
  return function sketch(p) {
    let particles = [];
    let zOffset = 0;

    p.setup = () => {
      p.createCanvas(width, height);
      particles = Array.from({ length: PARTICLE_COUNT }, () => ({
        x: p.random(width),
        y: p.random(height),
        color: p.random(PALETTE),
      }));
      p.background(BACKGROUND_COLOR);
    };

    p.draw = () => {
      p.noStroke();
      p.fill(5, 2, 8, 18);
      p.rect(0, 0, width, height);

      zOffset += 0.002;
      for (const particle of particles) {
        const angle =
          p.noise(particle.x * NOISE_SCALE, particle.y * NOISE_SCALE, zOffset) * p.TWO_PI * 3;
        particle.x += Math.cos(angle) * STEP_SIZE;
        particle.y += Math.sin(angle) * STEP_SIZE;
        if (particle.x < 0) particle.x += width;
        if (particle.x > width) particle.x -= width;
        if (particle.y < 0) particle.y += height;
        if (particle.y > height) particle.y -= height;

        p.fill(particle.color);
        p.circle(particle.x, particle.y, 2.5);
      }
    };
  };
}
```

Create `apps/noraebang-generative/static/sketches/particle-swarm.js` (Screen B — boids-like attractor swarm):

```js
import { PALETTE } from "../theme.js";

const BOID_COUNT = 90;
const MAX_SPEED = 2.4;
const NEIGHBOR_RADIUS = 60;

export function createSketch(width, height) {
  return function sketch(p) {
    let boids = [];

    p.setup = () => {
      p.createCanvas(width, height);
      boids = Array.from({ length: BOID_COUNT }, () => ({
        pos: p.createVector(p.random(width), p.random(height)),
        vel: p5.Vector.random2D().mult(MAX_SPEED),
        color: p.random(PALETTE),
      }));
      p.background(5, 2, 8);
    };

    p.draw = () => {
      p.noStroke();
      p.fill(5, 2, 8, 30);
      p.rect(0, 0, width, height);

      for (const boid of boids) {
        const alignment = p.createVector(0, 0);
        const cohesion = p.createVector(0, 0);
        const separation = p.createVector(0, 0);
        let neighborCount = 0;

        for (const other of boids) {
          if (other === boid) continue;
          const d = p.dist(boid.pos.x, boid.pos.y, other.pos.x, other.pos.y);
          if (d < NEIGHBOR_RADIUS) {
            alignment.add(other.vel);
            cohesion.add(other.pos);
            const away = p5.Vector.sub(boid.pos, other.pos).div(Math.max(d, 1));
            separation.add(away);
            neighborCount++;
          }
        }

        if (neighborCount > 0) {
          alignment.div(neighborCount).setMag(0.05);
          cohesion.div(neighborCount).sub(boid.pos).setMag(0.03);
          separation.setMag(0.08);
          boid.vel.add(alignment).add(cohesion).add(separation);
          boid.vel.limit(MAX_SPEED);
        }

        boid.pos.add(boid.vel);
        if (boid.pos.x < 0) boid.pos.x += width;
        if (boid.pos.x > width) boid.pos.x -= width;
        if (boid.pos.y < 0) boid.pos.y += height;
        if (boid.pos.y > height) boid.pos.y -= height;

        p.fill(boid.color);
        p.circle(boid.pos.x, boid.pos.y, 5);
      }
    };
  };
}
```

Create `apps/noraebang-generative/static/sketches/laser-grid.js` (Screen C — sweeping animated laser/grid lines):

```js
import { PALETTE } from "../theme.js";

const GRID_SPACING = 90;

export function createSketch(width, height) {
  return function sketch(p) {
    let t = 0;

    p.setup = () => {
      p.createCanvas(width, height);
    };

    p.draw = () => {
      p.background(5, 2, 8);
      t += 0.02;

      p.strokeWeight(2);
      for (let x = 0; x <= width; x += GRID_SPACING) {
        const wobble = Math.sin(t + x * 0.01) * 30;
        const color = PALETTE[Math.floor(x / GRID_SPACING) % PALETTE.length];
        p.stroke(color);
        p.line(x + wobble, 0, x - wobble, height);
      }

      const sweepY = ((Math.sin(t * 0.6) + 1) / 2) * height;
      p.strokeWeight(4);
      p.stroke(PALETTE[0]);
      p.line(0, sweepY, width, sweepY);
    };
  };
}
```

Create `apps/noraebang-generative/static/sketches/mirror-ball.js` (Screen D — rotating disco-ball sparkle/glint particles):

```js
import { PALETTE } from "../theme.js";

const FACET_COLS = 24;
const FACET_ROWS = 18;

export function createSketch(width, height) {
  return function sketch(p) {
    let t = 0;
    const cellW = width / FACET_COLS;
    const cellH = height / FACET_ROWS;

    p.setup = () => {
      p.createCanvas(width, height);
      p.noStroke();
    };

    p.draw = () => {
      p.background(3, 1, 5);
      t += 0.03;

      for (let row = 0; row < FACET_ROWS; row++) {
        for (let col = 0; col < FACET_COLS; col++) {
          const angle = p.noise(col * 0.3, row * 0.3, t) * p.TWO_PI;
          const glint = (Math.sin(angle * 4 + t * 2) + 1) / 2;
          if (glint > 0.82) {
            const color = PALETTE[(row + col) % PALETTE.length];
            p.fill(color);
            const size = p.map(glint, 0.82, 1, 2, Math.min(cellW, cellH) * 0.7);
            p.circle(col * cellW + cellW / 2, row * cellH + cellH / 2, size);
          }
        }
      }
    };
  };
}
```

Create `apps/noraebang-generative/static/sketches/vu-bars.js` (Screen A — non-reactive but rhythmic animated equalizer bars):

```js
import { PALETTE } from "../theme.js";

const BAR_COUNT = 32;

export function createSketch(width, height) {
  return function sketch(p) {
    let t = 0;
    const barWidth = width / BAR_COUNT;

    p.setup = () => {
      p.createCanvas(width, height);
      p.noStroke();
    };

    p.draw = () => {
      p.background(4, 2, 7);
      t += 0.05;

      for (let i = 0; i < BAR_COUNT; i++) {
        const level =
          ((Math.sin(t + i * 0.4) + 1) / 2) * 0.6 + ((Math.sin(t * 2.3 + i * 0.15) + 1) / 2) * 0.4;
        const barHeight = level * height;
        p.fill(PALETTE[i % PALETTE.length]);
        p.rect(i * barWidth + 2, height - barHeight, barWidth - 4, barHeight);
      }
    };
  };
}
```

Create `apps/noraebang-generative/static/sketches/scanline-crt.js` (Screen E — retro CRT scanline + VHS glitch texture):

```js
import { PALETTE } from "../theme.js";

const SCANLINE_SPACING = 4;

export function createSketch(width, height) {
  return function sketch(p) {
    let t = 0;

    p.setup = () => {
      p.createCanvas(width, height);
    };

    p.draw = () => {
      p.background(4, 2, 7);
      t += 1;

      p.noStroke();
      p.fill(255, 255, 255, 6);
      for (let y = 0; y < height; y += SCANLINE_SPACING) {
        p.rect(0, y, width, 1);
      }

      p.stroke(PALETTE[Math.floor(t / 40) % PALETTE.length]);
      p.strokeWeight(3);
      const glitchY = p.noise(t * 0.05) * height;
      const glitchOffset = (p.noise(t * 0.1) - 0.5) * 40;
      p.line(0, glitchY, width, glitchY + glitchOffset);

      if (p.random() < 0.03) {
        p.noStroke();
        p.fill(255, 255, 255, 20);
        p.rect(0, p.random(height), width, p.random(2, 12));
      }
    };
  };
}
```

- [ ] **Step 2: Manually verify all six sketches render without error**

No visible display is available to you, so verify with headless Playwright — this project's established pattern for DOM-dependent code.

Write a throwaway HTML file (don't commit it) at `apps/noraebang-generative/static/_sketch-check.html`:

```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <script src="/vendor/p5.min.js"></script>
  </head>
  <body>
    <script type="module">
      const sketches = [
        ["flow-field", 1800, 1400],
        ["particle-swarm", 1200, 600],
        ["laser-grid", 1200, 600],
        ["mirror-ball", 1600, 400],
        ["vu-bars", 1600, 400],
        ["scanline-crt", 1600, 400],
      ];
      for (const [name, width, height] of sketches) {
        const container = document.createElement("div");
        document.body.appendChild(container);
        const module = await import(`/sketches/${name}.js`);
        new p5(module.createSketch(width, height), container);
      }
      window.__sketchCheckDone = true;
    </script>
  </body>
</html>
```

Start the real server pointed at this app (`APP_DIR=$(pwd)/apps/noraebang-generative/static uv run python -m layout_server.main &`, capture the PID, `sleep 2`). Write a throwaway Python script using Playwright: launch headless chromium, `ignore_https_errors=True`, collect `page.on("console", ...)` messages of type `error`, navigate to `https://localhost:8443/_sketch-check.html`, wait ~3 seconds for `window.__sketchCheckDone` and a few draw frames, then assert `page.query_selector_all("canvas")` has length 6 and the collected error list is empty. Also take a screenshot (`page.screenshot(path="/tmp/noraebang-sketch-check.png")`) for a visual sanity check — you don't have to judge the aesthetics yourself, just confirm nothing is a solid black/blank rectangle (which would indicate a sketch silently failed to draw anything).

Kill the server, delete `apps/noraebang-generative/static/_sketch-check.html` and your throwaway Python script.

- [ ] **Step 3: Commit**

```bash
git add apps/noraebang-generative/static/sketches/
git commit -m "feat: add six generative sketches (flow-field, particle-swarm, laser-grid, mirror-ball, vu-bars, scanline-crt)"
```

---

## Task 3: Ambient placeholder track + `index.html` bootstrap

**Files:**
- Create: `apps/noraebang-generative/static/assets/track.mp3`
- Create: `apps/noraebang-generative/static/index.html`

**Interfaces:**
- Consumes: `initLayoutDriver`, `routeAudioElement` (framework, `static/layout-driver.js`); `validateSketchAssignments`, `sketchModulePath` (Task 1); all six `createSketch(width, height)` factories (Task 2).

- [ ] **Step 1: Generate the placeholder ambient track**

This is a simple three-note ambient drone (A2/E3/A3, a fifth+octave), faded in/out at the ends so `<audio loop>` doesn't click when it wraps — not a licensed music track, since sourcing real copyrighted audio isn't something to do without the user supplying it. Verified to produce a valid 30-second MP3:

```bash
mkdir -p apps/noraebang-generative/static/assets
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "sine=frequency=110:duration=30" \
  -f lavfi -i "sine=frequency=164.81:duration=30" \
  -f lavfi -i "sine=frequency=220:duration=30" \
  -filter_complex "[0:a]volume=0.25[a0];[1:a]volume=0.2[a1];[2:a]volume=0.15[a2];[a0][a1][a2]amix=inputs=3:duration=longest[mixed];[mixed]afade=t=in:st=0:d=2,afade=t=out:st=28:d=2[out]" \
  -map "[out]" -ac 2 -ar 44100 -b:a 128k apps/noraebang-generative/static/assets/track.mp3
```

Run: `ffprobe -hide_banner -v error -show_entries format=duration,format_name -of default=noprint_wrappers=1 apps/noraebang-generative/static/assets/track.mp3`
Expected: `format_name=mp3` and `duration=` close to `30.0`.

If `ffmpeg` isn't available in your environment, report BLOCKED with that specific finding rather than committing a missing/fake audio file.

- [ ] **Step 2: Write `index.html`**

Create `apps/noraebang-generative/static/index.html`:

```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Noraebang Generative Wall</title>
    <script src="/vendor/p5.min.js"></script>
  </head>
  <body>
    <script type="module">
      import { initLayoutDriver, routeAudioElement } from "/layout-driver.js";
      import { validateSketchAssignments, sketchModulePath } from "/sketch-loader.js";

      const driver = await initLayoutDriver();
      window.LayoutDriver = driver;

      const configResponse = await fetch("/config/noraebang.json");
      const config = await configResponse.json();
      const screens = validateSketchAssignments(config);

      for (const screen of screens) {
        try {
          const container = driver.getScreenContainer(screen.id);
          const module = await import(sketchModulePath(screen.sketch));
          const sketchFn = module.createSketch(container.width, container.height);
          new p5(sketchFn, container.element);
        } catch (error) {
          console.error(`Sketch "${screen.sketch}" failed to load for screen "${screen.id}"`, error);
        }
      }

      const audio = document.createElement("audio");
      audio.src = "/assets/track.mp3";
      audio.loop = true;
      audio.autoplay = true;
      document.body.appendChild(audio);
      routeAudioElement(audio);
    </script>
  </body>
</html>
```

Two things worth noting for your own understanding, not action items: (1) each sketch's `new p5(...)` construction is individually wrapped in try/catch, per spec §6 — a sketch module that throws during import or `setup()` only leaves its own screen dark; (2) `routeAudioElement(audio)` is called without `await` deliberately — nothing in this script runs after it, so there's no reason to block page setup on it, and per a known risk flagged during framework development (see Task 4), awaiting it could hang in some headless/capture contexts.

- [ ] **Step 3: Manually verify the full page loads and audio element exists**

Start the real server (`APP_DIR=$(pwd)/apps/noraebang-generative/static uv run python -m layout_server.main &`, capture PID, `sleep 2`). Using headless Playwright (throwaway script, not committed), load `https://localhost:8443/`, wait ~3 seconds, and confirm: `document.querySelectorAll("canvas").length === 6`, `document.querySelector("audio")` exists with `src` ending in `/assets/track.mp3` and `loop === true`, and no console errors were logged. Kill the server and delete the throwaway script afterward.

- [ ] **Step 4: Commit**

```bash
git add apps/noraebang-generative/static/assets/track.mp3 apps/noraebang-generative/static/index.html
git commit -m "feat: add placeholder ambient track and index.html bootstrap"
```

---

## Task 4: Framework fix — guard `routeAudioElement` against a `getUserMedia` hang

**Files:**
- Modify: `static/layout-driver.js`

**Interfaces:**
- Consumes/Modifies: `routeAudioElement(el)` (framework, already implemented and reviewed).

This is a framework file, not an app file — flagged explicitly since "apps are strictly isolated" is the norm in this repo. It's included here because it's a small, previously-known, previously-flagged risk (not new scope): during the framework's own Task 11 verification, `navigator.mediaDevices.getUserMedia({audio: true})` was found to hang indefinitely in a headless Chromium/Playwright context, worked around only in that task's throwaway verification script, not in the shipped code (see `.superpowers/sdd/task-11-report.md`'s "Concerns" section). This app is the first to actually rely on `routeAudioElement` running to completion in a real Chrome-driven capture context (the NDI broadcaster), so the un-fixed hang risk becomes live here.

- [ ] **Step 1: Read the current implementation**

Read `static/layout-driver.js`'s `routeAudioElement` function to confirm its current shape (it should match what's shown below — unchanged since the framework's original implementation).

- [ ] **Step 2: Add a timeout guard around the `getUserMedia` call**

Change:

```js
export async function routeAudioElement(el) {
  const response = await fetch("/api/audio-config");
  const config = await response.json();
  if (!config.enabled || !config.output_device) {
    return;
  }

  await navigator.mediaDevices.getUserMedia({ audio: true }).catch(() => null);
  const devices = await navigator.mediaDevices.enumerateDevices();
```

to:

```js
async function requestMicrophoneAccessWithTimeout(timeoutMs) {
  return Promise.race([
    navigator.mediaDevices.getUserMedia({ audio: true }).catch(() => null),
    new Promise((resolve) => setTimeout(() => resolve(null), timeoutMs)),
  ]);
}

export async function routeAudioElement(el) {
  const response = await fetch("/api/audio-config");
  const config = await response.json();
  if (!config.enabled || !config.output_device) {
    return;
  }

  await requestMicrophoneAccessWithTimeout(3000);
  const devices = await navigator.mediaDevices.enumerateDevices();
```

The permission request is still attempted first (still needed so `enumerateDevices()` returns real device labels when it succeeds), but `routeAudioElement` can now never hang longer than 3 seconds waiting on it — if the browser never resolves the permission prompt (as observed in headless/CDP-driven contexts), the timeout branch resolves `null` and the function proceeds to `enumerateDevices()` regardless (which will just return unlabeled devices in that case — `matchDeviceByName` against an empty/unlabeled `label` simply won't match, and the existing "device not found" warning path handles that gracefully, unchanged).

- [ ] **Step 3: Manually verify the fix actually resolves the hang**

Reproduce the original hang, then confirm the fix, using headless Playwright (this repeats the investigation from the framework's own Task 11, this time verifying a real fix rather than working around it in a throwaway script):

```bash
uv run python -m layout_server.main &
SERVER_PID=$!
sleep 2
```

Write a throwaway script that loads any page served by the running server (`https://localhost:8443/`, headless, `ignore_https_errors=True`), then in the page context calls `await routeAudioElement(new Audio())` (importing it from `/layout-driver.js`) with a wall-clock timer around the call, from the Python side (e.g. record `time.monotonic()` before and after the `page.evaluate(...)` call). Expected: the call completes in well under 3 seconds (typically near-instantly, since `config.enabled`/`output_device` likely won't match a real device on this dev machine and the function returns early at the "not found" branch — but even forcing past that point by testing `requestMicrophoneAccessWithTimeout` directly should complete within ~3 seconds, never hanging indefinitely as observed before the fix).

Kill the server and delete the throwaway script.

- [ ] **Step 4: Run the full test suite to confirm no regression**

Run: `uv run pytest -v` (expect all previously-passing tests still passing — this change doesn't touch any Python code) and `node --test static/*.test.mjs` (expect the existing 9 tests still passing — this change doesn't touch `geometry.js`/`device-match.js`).

- [ ] **Step 5: Commit**

```bash
git add static/layout-driver.js
git commit -m "fix: guard routeAudioElement's getUserMedia call with a timeout

Previously observed to hang indefinitely in headless/CDP-driven
Chrome contexts (flagged during the framework's own Task 11
verification but not fixed at the time). The noraebang-generative
app is the first to depend on this function completing in exactly
that kind of context (the NDI broadcaster's headed-but-automated
Chrome), making the risk live."
```

---

## Task 5: Framework fix — allow audio autoplay in the NDI broadcaster's Chrome launch

**Files:**
- Modify: `ndi_broadcaster/launcher.py`

**Interfaces:**
- Modifies: `_capture_loop`'s `playwright.chromium.launch(...)` call (framework, already implemented and reviewed).

Also a framework file, flagged for the same reason as Task 4. Chrome's default autoplay policy blocks unmuted `<audio autoplay>` unless the page has prior user interaction or the browser was launched with an explicit override — none of which apply to a fully automated kiosk page. Without this, `index.html`'s `audio.autoplay = true` (Task 3) will silently never actually play when driven by the real broadcaster.

- [ ] **Step 1: Read the current Chrome launch call**

Read `ndi_broadcaster/launcher.py`'s `_capture_loop` function to confirm the current `playwright.chromium.launch(...)` call — it should currently be `playwright.chromium.launch(headless=False, args=["--kiosk"])`.

- [ ] **Step 2: Add the autoplay policy flag**

Change:

```python
        browser = await playwright.chromium.launch(headless=False, args=["--kiosk"])
```

to:

```python
        browser = await playwright.chromium.launch(
            headless=False, args=["--kiosk", "--autoplay-policy=no-user-gesture-required"]
        )
```

- [ ] **Step 3: Manually verify the flag actually enables autoplay**

Headless Playwright, this time explicitly passing the same flag to your own test launch (unlike Task 2/3's verification scripts, which didn't need it since they weren't testing autoplay specifically):

```bash
uv run python -m layout_server.main &
SERVER_PID=$!
sleep 2
```

Write a throwaway script: launch headless chromium with `args=["--autoplay-policy=no-user-gesture-required"]`, load `https://localhost:8443/` pointed at the noraebang app (`APP_DIR` set accordingly when starting the server above), wait ~2 seconds, and check `page.evaluate("document.querySelector('audio').paused")`. Expected: `False` (audio actually started playing). As a control, also try launching WITHOUT the flag and confirm `paused` is `True` in that case — this proves the flag is what makes the difference, not something else about the page.

Kill the server, delete the throwaway script.

- [ ] **Step 4: Run the full test suite to confirm no regression**

Run: `uv run pytest -v` — expect all tests passing (this changes a Playwright launch argument, not any tested code path — no test directly exercises `_capture_loop`'s real Chrome launch, so this is about confirming nothing else broke).

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format ndi_broadcaster/launcher.py && uv run ruff check ndi_broadcaster/`

```bash
git add ndi_broadcaster/launcher.py
git commit -m "fix: allow audio autoplay in the NDI broadcaster's kiosk Chrome launch

Chrome's default autoplay policy blocks unmuted <audio autoplay> in a
fully automated kiosk page with no prior user interaction. Needed for
the noraebang-generative app's ambient track to actually play when
driven by the real broadcaster."
```

---

## Task 6: Committed Playwright smoke test

**Files:**
- Create: `apps/noraebang-generative/tests/test_smoke.py`

**Interfaces:**
- Consumes: `layout_server.main` (started as a real subprocess, not imported); no app-specific Python code (this app has none).

This is the one automated test the spec explicitly asks for (§9): "load the page headless, assert exactly 6 `<canvas>` elements exist... and that no console errors were logged during a short run." Unlike the throwaway verification scripts in Tasks 2/3/4/5, this is a permanent, committed, repeatable test — it needs a real running server, so it starts one as a subprocess rather than reusing the in-process `TestClient` pattern (which doesn't serve real static files over a real port the way a browser needs).

- [ ] **Step 1: Write the test**

```bash
mkdir -p apps/noraebang-generative/tests
```

Create `apps/noraebang-generative/tests/test_smoke.py`:

```python
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

APP_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_ROOT.parent.parent


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def running_server(tmp_path):
    port = _find_free_port()
    env = {
        **os.environ,
        "APP_DIR": str(APP_ROOT / "static"),
        "LAYOUT_DRIVER_HOST": "127.0.0.1",
        "LAYOUT_DRIVER_PORT": str(port),
        "LAYOUT_DRIVER_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "layout_server.main"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        url = f"https://127.0.0.1:{port}/healthz"
        deadline = time.monotonic() + 15
        healthy = False
        while time.monotonic() < deadline:
            try:
                if httpx.get(url, verify=False, timeout=1.0).status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        if not healthy:
            raise RuntimeError("server did not become healthy in time")
        yield f"https://127.0.0.1:{port}/"
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_page_creates_six_canvases_with_no_console_errors_and_plays_audio(running_server):
    console_errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page.goto(running_server)
        page.wait_for_timeout(3000)

        canvases = page.query_selector_all("canvas")
        assert len(canvases) == 6

        audio_paused = page.evaluate("document.querySelector('audio').paused")
        assert audio_paused is False

        browser.close()

    assert console_errors == []
```

- [ ] **Step 2: Run test to verify it fails first for the right reason, then passes**

Run: `uv run pytest apps/noraebang-generative/tests/test_smoke.py -v -s`

If Tasks 1–5 are all already in place (which they should be by this point in the plan), this should PASS on the first run — there's no "before" code to be red against, since this test exercises the finished app rather than driving new production code into existence (unlike the TDD tasks earlier in this plan). Confirm it passes:

Expected: 1 passed. If it fails, the failure message will tell you which assertion — most likely candidates: fewer than 6 canvases (a sketch failed silently — check the server's stdout, captured in `process.stdout`, for the `console.error` from `index.html`'s try/catch), or `audio_paused` is `True` (Task 5's autoplay flag isn't actually being applied to this test's own browser launch — check the `args=` list matches Task 5's verified flag exactly).

- [ ] **Step 3: Lint and commit**

Run: `uv run ruff format apps/noraebang-generative/tests/test_smoke.py && uv run ruff check apps/noraebang-generative/`

```bash
git add apps/noraebang-generative/tests/test_smoke.py
git commit -m "test: add committed Playwright smoke test for the noraebang-generative app"
```

---

## Self-Review Notes

- **Spec coverage:** §2 (architecture: index.html/theme.js/sketches/config) → Tasks 1, 3; §3 (sketch roster + config) → Tasks 1, 2; §4 (wiring to the framework) → Task 3; §5 (audio) → Tasks 3, 4, 5; §6 (robustness: per-sketch try/catch) → Task 3; §7 (performance budget) → Task 2's sketch implementations (bounded loops, no per-pixel ops); §9 (testing: config/sketch-mapping unit test, Playwright smoke test) → Tasks 1, 6.
- **Documented deviations from the spec's literal text**, both explained in Global Constraints and necessary given how the framework actually serves static apps: JSON instead of YAML for the browser-read config; everything nested under `static/` rather than as siblings of it.
- **Two framework files get touched** (Tasks 4, 5) despite "apps are strictly isolated" — both are pre-existing, already-flagged risks in framework code that this app is the first to actually exercise in a failure-prone way, not new app-specific logic leaking into the framework.
- **Interface consistency check:** every sketch module's exported `createSketch(width, height)` signature (Task 2) matches exactly how `index.html` (Task 3) calls it (`module.createSketch(container.width, container.height)`). `sketchModulePath`/`validateSketchAssignments` (Task 1) are used with the same signatures in both the unit test and `index.html`.

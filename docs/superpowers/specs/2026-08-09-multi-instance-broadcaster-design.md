# Multi-Instance Support — Design Spec

## 1. Purpose

Today, `layout-driver` assumes exactly one instance (one `layout_server` + one
`ndi_broadcaster`, serving one app/layout) runs on a machine at a time. Several
of its config paths and identifiers are either hardcoded or only work by
accident of `run.sh`'s current working directory. This spec makes it possible
to run multiple independent instances — each driving a different wall/layout,
with its own NDI source, its own audio device pair, and its own runtime
files — side by side on one machine, with no manual code changes and no risk
of one instance silently stomping another's state.

**Use case in scope:** independent walls/apps running side by side (e.g. a
flux-gallery instance and a noraebang-generative instance, or two differently
laid-out flux-gallery instances, on the same machine at the same time), each
fully isolated from the others.

**Not in scope:** a single instance driving multiple layouts, or any kind of
shared/synchronized state *between* instances — every instance is fully
independent, unaware of any other instance's existence.

## 2. Current gaps (why this doesn't already work)

Verified against the current codebase, not from memory of an earlier pass:

- `layout_server`'s own config paths (`SCREENS_YAML`, `AUDIO_YAML` — read in
  `layout_server/main.py`), host/port (`LAYOUT_DRIVER_HOST`/`LAYOUT_DRIVER_PORT`),
  and cert/runtime-dir paths (`LAYOUT_DRIVER_RUNTIME_DIR`,
  `LAYOUT_DRIVER_SSL_CERT`/`_KEY`) are **already env-overridable**. Nothing to
  fix here.
- `ndi_broadcaster/launcher.py`'s `run()` defaults its two config paths —
  `config/broadcaster.yaml` and `config/audio.yaml` — as plain relative-path
  function-parameter defaults, with **no environment override at all**, and
  **not anchored to a `REPO_ROOT` constant** the way `layout_server/main.py`
  is. They only resolve correctly today because `run.sh` happens to `cd` into
  the repo root before launching the broadcaster. Nothing threads an
  instance-specific path to either of them.
- `config/broadcaster.yaml`'s `ndi_source_name` field (default `"Layout
  Driver"`) has no override mechanism. Two instances left at the default, or
  sharing one `broadcaster.yaml`, would both advertise the identical NDI
  source name on the network — ambiguous for any receiver/monitor doing
  name-based selection.
- No log line, in either `ndi_broadcaster` or `layout_server`, identifies
  which instance produced it. Interleaved output from two instances running
  in the same terminal/log aggregator is indistinguishable.

## 3. Design

One instance is fully described by **one config directory + one port**.

### 3.1 `ndi_broadcaster/launcher.py`

- Add a module-level `REPO_ROOT` constant (matching `layout_server/main.py`'s
  existing pattern) and anchor both config path defaults to it, so they no
  longer depend on the launching process's current working directory.
- Add an env override for the broadcaster config path: `BROADCASTER_YAML`
  (new name, following the existing `SCREENS_YAML`/`PROMPTS_YAML` convention).
- Read the *existing* `AUDIO_YAML` env var (already read server-side in
  `layout_server/main.py`) for the broadcaster's own audio config path,
  instead of the current unconditional hardcoded default. This closes the gap
  with the same name already in use, rather than introducing a second name
  for the same file.
- NDI source-name collisions are solved as a consequence of the above, with
  no new mechanism: once each instance points `BROADCASTER_YAML` at its own
  file, that file's existing `ndi_source_name` field just needs to differ per
  instance, exactly like `screens.yaml`/`audio.yaml` already work.
- Add a log format prefix showing the resolved port, read from the
  `LAYOUT_DRIVER_PORT` env var (already exported by `run.sh` into this
  process's environment) — cosmetic, for readable interleaved log output
  across instances. If unset, prefix is omitted (matches today's format
  exactly, for any invocation outside `run.sh`).

### 3.2 `layout_server`

- Add the same port-based log-line prefix to `layout_server`'s own log setup,
  for consistency. (uvicorn's own access logs already show host:port; this
  covers the framework's own log lines, which today have no prefix — or any
  explicit logging setup — at all.)

### 3.3 `run.sh`

- Add a new optional env var, `LAYOUT_DRIVER_CONFIG_DIR` (default: `config`
  at the repo root — i.e. **exactly today's behavior** when left unset).
- When resolving `SCREENS_YAML`, `AUDIO_YAML`, and the new `BROADCASTER_YAML`,
  default each to `<config-dir>/<file>.yaml` — but only for whichever of
  those aren't *already* individually set in the environment. Individual
  overrides always take precedence over the config-dir convenience default,
  preserving full manual control.
- Default `LAYOUT_DRIVER_RUNTIME_DIR` (if not independently set) to
  `runtime-<basename of config dir>` at the repo root, so cert/key files and
  `audio_devices.json` don't collide between instances either. When
  `LAYOUT_DRIVER_CONFIG_DIR` is left at its default (`config`), this resolves
  to the existing flat `runtime/` directory unchanged.

### 3.4 Example: two instances side by side

```bash
# Instance 1: flux-gallery on 8443, using config/ (today's default location)
./run.sh apps/flux-gallery/static

# Instance 2: noraebang-generative on 8444, using its own config directory
mkdir -p instances/noraebang
cp config/screens.yaml instances/noraebang/screens.yaml      # edited for this wall's geometry
cp config/broadcaster.yaml instances/noraebang/broadcaster.yaml  # edited: distinct ndi_source_name
cp config/audio.yaml instances/noraebang/audio.yaml          # edited: distinct input/output device names
LAYOUT_DRIVER_CONFIG_DIR=instances/noraebang \
LAYOUT_DRIVER_PORT=8444 \
  ./run.sh apps/noraebang-generative/static
```

Each instance's worker (if the app has one) is launched separately, as
today, and must independently be pointed at the matching instance — e.g. for
a second flux-gallery instance's worker: `SCREENS_YAML=instances/gallery2/screens.yaml
LAYOUT_DRIVER_URL=https://localhost:8444 GEMINI_API_KEY=... apps/flux-gallery/run.sh`.
A worker launched in a separate shell does not automatically inherit env vars
set inside `run.sh` — this is already true today for the single-instance
case, and remains the operator's responsibility here, not something this
design automates.

## 4. Explicit non-goals

- **Audio device provisioning.** Two instances must not share one virtual
  audio device (e.g. both pointed at `"BlackHole 2ch"`) — their browsers'
  audio outputs would mix together before either broadcaster captures it.
  This design makes device *names* independently configurable per instance
  (via each instance's own `audio.yaml`, already possible once §3.1's
  `AUDIO_YAML` gap is closed); it does not, and cannot, provision additional
  virtual audio devices. Setting up N distinct virtual devices (e.g. a second
  BlackHole instance, or a multi-channel variant) is an operator/deployment
  prerequisite, not something this framework automates.
- **GPU contention.** Apple Silicon has one GPU. Two GPU-heavy apps (e.g. two
  concurrent flux-gallery instances) will contend for it. Not addressed by
  this design — a hardware constraint, not a software one.
- **App-level writable state.** Any state an app's own worker writes (e.g.
  flux-gallery's `apps/flux-gallery/output/` image history, hardcoded and not
  env-overridable today) is that app's own responsibility to scope per
  instance if it needs to run multiple times concurrently — following the
  same pattern its worker already uses for `SCREENS_YAML`/`PROMPTS_YAML`.
  This design does not add such scoping generically.
- **Env var naming consistency across apps.** The framework's own
  `LAYOUT_DRIVER_TARGET_URL` (read by `ndi_broadcaster`, derived and exported
  by `run.sh`) and an app's own convention for the same concept (e.g.
  flux-gallery's worker reads `LAYOUT_DRIVER_URL`, a different name) are not
  unified here. Apps are free to define their own env vars; this design does
  not touch app code.
- **Process supervision/orchestration.** No new mechanism starts, stops, or
  monitors the 2–3 processes that make up one instance as a group. Operators
  start and stop each process themselves, exactly as today — just now safely
  parameterizable per instance.

## 5. Testing

- `ndi_broadcaster/launcher.py`'s config-path resolution (`REPO_ROOT`
  anchoring + env override precedence) is unit-testable the same way
  `layout_server/config.py`'s loaders already are: no live browser or NDI
  connection needed to verify the correct path is chosen.
- `run.sh`'s config-dir-to-individual-env-var default resolution is shell
  logic; verify with a smoke test that runs it with `LAYOUT_DRIVER_CONFIG_DIR`
  set against a temporary directory and confirms the three YAML env vars and
  the runtime dir resolve as designed, without actually starting a browser.
- No new automated test can cover the actual multi-instance *runtime*
  behavior (two real NDI sources on the network, two real audio devices) —
  that remains a manual, live verification step, consistent with how
  `ndi_broadcaster`'s other capture-backend work in this repo has always been
  verified.

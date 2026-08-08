# Layout Driver Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the reusable layout-driver framework — screen geometry config, a FastAPI server that brokers both pushed images and direct-render canvases across 6 independently-addressable screens inside one 3840×2160 page, an NDI broadcaster that captures and streams that page, and an audio loopback path — per `docs/superpowers/specs/2026-08-08-layout-driver-framework-design.md`.

**Architecture:** `layout_server` (FastAPI, HTTPS) serves the currently-active app's static bundle plus a shared client library (`static/layout-driver.js`); it exposes screen geometry, an image push/pull API with a WebSocket relay, a full-wall screenshot round trip, and audio device discovery/config. `ndi_broadcaster` drives headed Chrome via Playwright against that server, captures the page (CDP screencast by default, ScreenCaptureKit opt-in), and sends 3840×2160@30fps video plus a loopback-captured audio track via `cyndilib`. Sample apps (built in later, separate plans) live under `apps/<name>/` and only ever consume the framework's HTTP/JS/WS surface.

**Tech Stack:** Python 3.13, managed with `uv`; FastAPI + uvicorn; Pydantic v2 for config models; PyYAML; Pillow; `sounddevice`; `cyndilib`; Playwright; `cryptography` for the self-signed cert; `ruff` for lint/format; `pytest` for Python tests; native ES modules (no bundler) for the client, tested with Node's built-in test runner (`node --test`).

## Global Constraints

- Python 3.13 minimum, dependency/venv management via `uv` (not pip/poetry directly).
- Config files are YAML (`config/screens.yaml`, `config/audio.yaml`, `config/broadcaster.yaml`).
- No JS build step — client code is plain ES modules loaded via `<script type="module">`.
- No auth on any HTTP/WS endpoint (trusted-LAN kiosk assumption, per spec §8).
- Every screen id, rect, and config shape must match the exact fixture table in spec §2 — this is the canonical source of truth for expected values in tests.
- `ruff format` and `ruff check` must pass on all Python files touched; keep line length at 100.

---

## Task 1: Project scaffolding + screen geometry config

**Files:**
- Create: `pyproject.toml`
- Create: `config/screens.yaml`
- Create: `layout_server/__init__.py`
- Create: `layout_server/config.py`
- Create: `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `layout_server.config.ScreenGrid`, `ScreenRect`, `ScreenConfig`, `CanvasConfig`, `LayoutOffset`, `LayoutConfig` (Pydantic models); `LayoutConfig.screen_by_id(screen_id: str) -> ScreenConfig | None`; `compute_rect(grid: ScreenGrid, module_size: int, offset: LayoutOffset) -> ScreenRect`; `load_layout_config(path: Path) -> LayoutConfig`; `OverlappingScreensError(ValueError)`.

- [ ] **Step 1: Create the `uv` project**

Create `pyproject.toml`:

```toml
[project]
name = "layout-driver"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pyyaml>=6.0",
    "pydantic>=2.9",
    "pillow>=11.0",
    "sounddevice>=0.5",
    "cyndilib>=0.0.9",
    "playwright>=1.48",
    "httpx>=0.27",
    "cryptography>=43.0",
    "numpy>=2.1",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "ruff>=0.7",
]

[tool.ruff]
line-length = 100
target-version = "py313"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Run: `uv sync`
Expected: creates `.venv/` and `uv.lock` with no errors.

- [ ] **Step 2: Create the screens config fixture**

Create `config/screens.yaml`:

```yaml
canvas:
  width: 3840
  height: 2160
  fps: 30

module_size: 200
layout_offset: {x: 220, y: 80}

screens:
  - id: F
    name: "Screen F"
    grid: {col: 0, row: 0, cols: 9, rows: 7}
  - id: B
    name: "Screen B"
    grid: {col: 9, row: 0, cols: 6, rows: 3}
  - id: C
    name: "Screen C"
    grid: {col: 9, row: 3, cols: 6, rows: 3}
  - id: D
    name: "Screen D"
    grid: {col: 9, row: 6, cols: 8, rows: 2}
  - id: A
    name: "Screen A"
    grid: {col: 9, row: 8, cols: 8, rows: 2}
  - id: E
    name: "Screen E"
    grid: {col: 1, row: 7, cols: 8, rows: 2}
```

- [ ] **Step 3: Write the failing test**

Create `tests/__init__.py` (empty file).

Create `tests/test_config.py`:

```python
from pathlib import Path

import pytest

from layout_server.config import (
    CanvasConfig,
    LayoutOffset,
    OverlappingScreensError,
    ScreenGrid,
    compute_rect,
    load_layout_config,
)

SCREENS_YAML = Path(__file__).resolve().parent.parent / "config" / "screens.yaml"

EXPECTED_RECTS = {
    "F": (220, 80, 1800, 1400),
    "B": (2020, 80, 1200, 600),
    "C": (2020, 680, 1200, 600),
    "D": (2020, 1280, 1600, 400),
    "A": (2020, 1680, 1600, 400),
    "E": (420, 1480, 1600, 400),
}


def test_load_layout_config_computes_expected_rects():
    config = load_layout_config(SCREENS_YAML)

    assert config.canvas == CanvasConfig(width=3840, height=2160, fps=30)
    assert len(config.screens) == 6

    for screen in config.screens:
        expected_x, expected_y, expected_w, expected_h = EXPECTED_RECTS[screen.id]
        assert screen.rect.x == expected_x
        assert screen.rect.y == expected_y
        assert screen.rect.width == expected_w
        assert screen.rect.height == expected_h


def test_screen_by_id_returns_none_for_unknown_id():
    config = load_layout_config(SCREENS_YAML)
    assert config.screen_by_id("Z") is None
    assert config.screen_by_id("F") is not None


def test_compute_rect_applies_offset_and_module_size():
    grid = ScreenGrid(col=2, row=3, cols=4, rows=5)
    rect = compute_rect(grid, module_size=200, offset=LayoutOffset(x=10, y=20))
    assert rect.x == 10 + 2 * 200
    assert rect.y == 20 + 3 * 200
    assert rect.width == 4 * 200
    assert rect.height == 5 * 200


def test_overlapping_screens_are_rejected(tmp_path):
    overlapping_yaml = tmp_path / "overlapping.yaml"
    overlapping_yaml.write_text(
        """
canvas: {width: 3840, height: 2160, fps: 30}
module_size: 200
layout_offset: {x: 0, y: 0}
screens:
  - id: "1"
    name: "One"
    grid: {col: 0, row: 0, cols: 4, rows: 4}
  - id: "2"
    name: "Two"
    grid: {col: 2, row: 2, cols: 4, rows: 4}
"""
    )
    with pytest.raises(OverlappingScreensError):
        load_layout_config(overlapping_yaml)
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'layout_server'` (or `ImportError`).

- [ ] **Step 5: Implement `layout_server/config.py`**

Create `layout_server/__init__.py` (empty file).

Create `layout_server/config.py`:

```python
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class ScreenGrid(BaseModel):
    col: int
    row: int
    cols: int
    rows: int


class ScreenRect(BaseModel):
    x: int
    y: int
    width: int
    height: int


class ScreenConfig(BaseModel):
    id: str
    name: str
    grid: ScreenGrid
    rect: ScreenRect


class CanvasConfig(BaseModel):
    width: int
    height: int
    fps: int


class LayoutOffset(BaseModel):
    x: int
    y: int


class LayoutConfig(BaseModel):
    canvas: CanvasConfig
    module_size: int
    layout_offset: LayoutOffset
    screens: list[ScreenConfig]

    def screen_by_id(self, screen_id: str) -> ScreenConfig | None:
        return next((screen for screen in self.screens if screen.id == screen_id), None)


class OverlappingScreensError(ValueError):
    pass


def compute_rect(grid: ScreenGrid, module_size: int, offset: LayoutOffset) -> ScreenRect:
    return ScreenRect(
        x=offset.x + grid.col * module_size,
        y=offset.y + grid.row * module_size,
        width=grid.cols * module_size,
        height=grid.rows * module_size,
    )


def _rects_overlap(a: ScreenRect, b: ScreenRect) -> bool:
    return (
        a.x < b.x + b.width
        and b.x < a.x + a.width
        and a.y < b.y + b.height
        and b.y < a.y + a.height
    )


def load_layout_config(path: Path) -> LayoutConfig:
    raw = yaml.safe_load(path.read_text())
    offset = LayoutOffset(**raw["layout_offset"])
    module_size = raw["module_size"]
    canvas = CanvasConfig(**raw["canvas"])

    screens: list[ScreenConfig] = []
    for entry in raw["screens"]:
        grid = ScreenGrid(**entry["grid"])
        rect = compute_rect(grid, module_size, offset)
        screens.append(ScreenConfig(id=entry["id"], name=entry["name"], grid=grid, rect=rect))

    for i, a in enumerate(screens):
        for b in screens[i + 1 :]:
            if _rects_overlap(a.rect, b.rect):
                raise OverlappingScreensError(f"Screens {a.id!r} and {b.id!r} overlap")

    return LayoutConfig(canvas=canvas, module_size=module_size, layout_offset=offset, screens=screens)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: 4 passed.

- [ ] **Step 7: Lint and commit**

Run: `uv run ruff format layout_server/ tests/ && uv run ruff check layout_server/ tests/`
Expected: no errors (format may reformat files — re-run check after).

```bash
git add pyproject.toml config/screens.yaml layout_server/ tests/ uv.lock
git commit -m "feat: add screen geometry config model and loader"
```

---

## Task 2: Audio config, device discovery, and name matching

**Files:**
- Create: `config/audio.yaml`
- Create: `layout_server/audio.py`
- Test: `tests/test_audio.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `layout_server.audio.AudioDevice`, `AudioDeviceList`, `AudioConfig` (Pydantic models); `load_audio_config(path: Path) -> AudioConfig`; `discover_audio_devices() -> AudioDeviceList`; `write_audio_devices_file(devices: AudioDeviceList, path: Path) -> None`; `match_device_by_name(name: str, devices: list[AudioDevice]) -> AudioDevice | None`.

- [ ] **Step 1: Create the audio config fixture**

Create `config/audio.yaml`:

```yaml
enabled: true
input_device: "BlackHole 2ch"
output_device: "BlackHole 2ch"
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_audio.py`:

```python
from pathlib import Path

from layout_server.audio import (
    AudioConfig,
    AudioDevice,
    discover_audio_devices,
    load_audio_config,
    match_device_by_name,
    write_audio_devices_file,
)

AUDIO_YAML = Path(__file__).resolve().parent.parent / "config" / "audio.yaml"


def test_load_audio_config():
    config = load_audio_config(AUDIO_YAML)
    assert config == AudioConfig(enabled=True, input_device="BlackHole 2ch", output_device="BlackHole 2ch")


def test_match_device_by_name_exact_case_insensitive():
    devices = [
        AudioDevice(index=0, name="BlackHole 2ch", max_input_channels=2),
        AudioDevice(index=1, name="MacBook Pro Speakers", max_output_channels=2),
    ]
    match = match_device_by_name("blackhole 2ch", devices)
    assert match is not None
    assert match.index == 0


def test_match_device_by_name_substring():
    devices = [AudioDevice(index=0, name="BlackHole 2ch", max_input_channels=2)]
    match = match_device_by_name("blackhole", devices)
    assert match is not None
    assert match.index == 0


def test_match_device_by_name_not_found_returns_none():
    devices = [AudioDevice(index=0, name="BlackHole 2ch", max_input_channels=2)]
    assert match_device_by_name("nonexistent device", devices) is None


def test_match_device_by_name_empty_name_returns_none():
    devices = [AudioDevice(index=0, name="BlackHole 2ch", max_input_channels=2)]
    assert match_device_by_name("", devices) is None


def test_discover_audio_devices_splits_inputs_and_outputs(monkeypatch):
    fake_devices = [
        {"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2},
        {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0},
    ]

    class _FakeSoundDevice:
        @staticmethod
        def query_devices():
            return fake_devices

    monkeypatch.setattr("layout_server.audio.sd", _FakeSoundDevice)

    devices = discover_audio_devices()

    assert [d.name for d in devices.inputs] == ["BlackHole 2ch", "MacBook Pro Microphone"]
    assert [d.name for d in devices.outputs] == ["BlackHole 2ch", "MacBook Pro Speakers"]
    assert devices.inputs[0].index == 0
    assert devices.outputs[1].index == 1


def test_write_audio_devices_file(tmp_path):
    devices = discover_audio_devices.__wrapped__() if hasattr(discover_audio_devices, "__wrapped__") else None
    from layout_server.audio import AudioDeviceList

    devices = AudioDeviceList(
        inputs=[AudioDevice(index=0, name="BlackHole 2ch", max_input_channels=2)],
        outputs=[AudioDevice(index=0, name="BlackHole 2ch", max_output_channels=2)],
    )
    out_path = tmp_path / "audio_devices.json"
    write_audio_devices_file(devices, out_path)

    assert out_path.exists()
    assert "BlackHole 2ch" in out_path.read_text()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_audio.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'layout_server.audio'`.

- [ ] **Step 4: Implement `layout_server/audio.py`**

```python
from __future__ import annotations

from pathlib import Path

import sounddevice as sd
import yaml
from pydantic import BaseModel


class AudioDevice(BaseModel):
    index: int
    name: str
    max_input_channels: int = 0
    max_output_channels: int = 0


class AudioDeviceList(BaseModel):
    inputs: list[AudioDevice]
    outputs: list[AudioDevice]


class AudioConfig(BaseModel):
    enabled: bool = True
    input_device: str = ""
    output_device: str = ""


def load_audio_config(path: Path) -> AudioConfig:
    raw = yaml.safe_load(path.read_text())
    return AudioConfig(**raw)


def discover_audio_devices() -> AudioDeviceList:
    inputs: list[AudioDevice] = []
    outputs: list[AudioDevice] = []
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0:
            inputs.append(
                AudioDevice(index=index, name=device["name"], max_input_channels=device["max_input_channels"])
            )
        if device["max_output_channels"] > 0:
            outputs.append(
                AudioDevice(index=index, name=device["name"], max_output_channels=device["max_output_channels"])
            )
    return AudioDeviceList(inputs=inputs, outputs=outputs)


def write_audio_devices_file(devices: AudioDeviceList, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(devices.model_dump_json(indent=2))


def match_device_by_name(name: str, devices: list[AudioDevice]) -> AudioDevice | None:
    if not name:
        return None
    lowered = name.lower()
    for device in devices:
        if lowered in device.name.lower():
            return device
    return None
```

Note: `import sounddevice as sd` requires PortAudio to be installed on the machine (it is a compiled dependency). If `uv sync` / import fails on your machine because PortAudio isn't installed, install it first (macOS: `brew install portaudio`), then retry.

Remove the dead `test_write_audio_devices_file`'s unused `discover_audio_devices.__wrapped__()` line — clean up the test to just construct `AudioDeviceList` directly (already shown above; simplify by deleting that first probing line since `discover_audio_devices` isn't wrapped):

```python
def test_write_audio_devices_file(tmp_path):
    devices = AudioDeviceList(
        inputs=[AudioDevice(index=0, name="BlackHole 2ch", max_input_channels=2)],
        outputs=[AudioDevice(index=0, name="BlackHole 2ch", max_output_channels=2)],
    )
    out_path = tmp_path / "audio_devices.json"
    write_audio_devices_file(devices, out_path)

    assert out_path.exists()
    assert "BlackHole 2ch" in out_path.read_text()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_audio.py -v`
Expected: 6 passed.

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff format layout_server/audio.py tests/test_audio.py && uv run ruff check layout_server/ tests/`

```bash
git add config/audio.yaml layout_server/audio.py tests/test_audio.py
git commit -m "feat: add audio config, device discovery, and name matching"
```

---

## Task 3: Self-signed cert bootstrap

**Files:**
- Create: `layout_server/certs.py`
- Test: `tests/test_certs.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `layout_server.certs.ensure_self_signed_cert(cert_path: Path, key_path: Path) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_certs.py`:

```python
from pathlib import Path

from cryptography import x509

from layout_server.certs import ensure_self_signed_cert


def test_ensure_self_signed_cert_creates_files(tmp_path):
    cert_path = tmp_path / "certs" / "cert.pem"
    key_path = tmp_path / "certs" / "key.pem"

    ensure_self_signed_cert(cert_path, key_path)

    assert cert_path.exists()
    assert key_path.exists()

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    common_name = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
    assert common_name == "localhost"


def test_ensure_self_signed_cert_is_idempotent(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    ensure_self_signed_cert(cert_path, key_path)
    first_cert_bytes = cert_path.read_bytes()

    ensure_self_signed_cert(cert_path, key_path)
    second_cert_bytes = cert_path.read_bytes()

    assert first_cert_bytes == second_cert_bytes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_certs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'layout_server.certs'`.

- [ ] **Step 3: Implement `layout_server/certs.py`**

```python
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def ensure_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    if cert_path.exists() and key_path.exists():
        return

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    san = x509.SubjectAlternativeName(
        [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_certs.py -v`
Expected: 2 passed.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format layout_server/certs.py tests/test_certs.py && uv run ruff check layout_server/ tests/`

```bash
git add layout_server/certs.py tests/test_certs.py
git commit -m "feat: add self-signed cert bootstrap"
```

---

## Task 4: FastAPI app skeleton (health, screens, audio config/devices)

**Files:**
- Create: `layout_server/app.py`
- Create: `tests/conftest.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `layout_server.config.LayoutConfig`, `load_layout_config` (Task 1); `layout_server.audio.AudioConfig`, `AudioDeviceList`, `load_audio_config`, `discover_audio_devices`, `write_audio_devices_file` (Task 2).
- Produces: `layout_server.app.AppState` (dataclass: `layout_config`, `audio_config`, `audio_devices`, `runtime_dir`); `create_app(*, screens_yaml: Path, audio_yaml: Path, runtime_dir: Path, app_static_dir: Path, framework_static_dir: Path) -> FastAPI`. Later tasks (5, 6) extend `create_app` in place.

- [ ] **Step 1: Write the failing test**

Create `tests/conftest.py`:

```python
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from layout_server.app import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app_static_dir(tmp_path: Path) -> Path:
    static_dir = tmp_path / "app-static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><html><body>test app</body></html>")
    return static_dir


@pytest.fixture
def client(tmp_path: Path, app_static_dir: Path) -> TestClient:
    app = create_app(
        screens_yaml=REPO_ROOT / "config" / "screens.yaml",
        audio_yaml=REPO_ROOT / "config" / "audio.yaml",
        runtime_dir=tmp_path / "runtime",
        app_static_dir=app_static_dir,
        framework_static_dir=REPO_ROOT / "static",
    )
    with TestClient(app) as test_client:
        yield test_client
```

Create `tests/test_app.py`:

```python
def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_screens_returns_computed_rects(client):
    response = client.get("/api/screens")
    assert response.status_code == 200
    body = response.json()
    screen_f = next(s for s in body["screens"] if s["id"] == "F")
    assert screen_f["rect"] == {"x": 220, "y": 80, "width": 1800, "height": 1400}


def test_get_audio_config(client):
    response = client.get("/api/audio-config")
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "input_device": "BlackHole 2ch", "output_device": "BlackHole 2ch"}


def test_get_audio_devices_populated_by_lifespan(client, tmp_path, monkeypatch):
    response = client.get("/api/audio-devices")
    assert response.status_code == 200
    body = response.json()
    assert "inputs" in body
    assert "outputs" in body


def test_audio_devices_file_written_on_startup(client, tmp_path):
    devices_file = tmp_path / "runtime" / "audio_devices.json"
    assert devices_file.exists()
```

Note: `test_get_audio_devices_populated_by_lifespan` and `test_audio_devices_file_written_on_startup` exercise the real `discover_audio_devices()` (real PortAudio device list on the machine running the tests) rather than a mock — this is intentional here since we're testing that the *lifespan wiring* calls it and persists the result, not the discovery logic itself (already unit-tested with a fake in Task 2). This will work on any machine with at least a default audio device, which is effectively all of them.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'layout_server.app'`.

- [ ] **Step 3: Implement `layout_server/app.py`**

```python
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .audio import AudioConfig, AudioDeviceList, discover_audio_devices, load_audio_config, write_audio_devices_file
from .config import LayoutConfig, load_layout_config


@dataclass
class AppState:
    layout_config: LayoutConfig
    audio_config: AudioConfig
    runtime_dir: Path
    audio_devices: AudioDeviceList | None = None


def create_app(
    *,
    screens_yaml: Path,
    audio_yaml: Path,
    runtime_dir: Path,
    app_static_dir: Path,
    framework_static_dir: Path,
) -> FastAPI:
    state = AppState(
        layout_config=load_layout_config(screens_yaml),
        audio_config=load_audio_config(audio_yaml),
        runtime_dir=runtime_dir,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.audio_devices = discover_audio_devices()
        state.runtime_dir.mkdir(parents=True, exist_ok=True)
        write_audio_devices_file(state.audio_devices, state.runtime_dir / "audio_devices.json")
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.layout = state

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/screens")
    async def get_screens() -> LayoutConfig:
        return state.layout_config

    @app.get("/api/audio-config")
    async def get_audio_config() -> AudioConfig:
        return state.audio_config

    @app.get("/api/audio-devices")
    async def get_audio_devices() -> AudioDeviceList:
        return state.audio_devices

    @app.get("/layout-driver.js")
    async def get_layout_driver_js() -> FileResponse:
        return FileResponse(framework_static_dir / "layout-driver.js", media_type="text/javascript")

    app.mount("/", StaticFiles(directory=app_static_dir, html=True), name="app")

    return app
```

Since `static/layout-driver.js` doesn't exist yet (it's created in Task 9), also create a placeholder now so `framework_static_dir` resolves for tests that don't hit `/layout-driver.js` directly (none currently do, but keep the directory real):

```bash
mkdir -p static
touch static/.gitkeep
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py tests/test_config.py tests/test_audio.py tests/test_certs.py -v`
Expected: all passed.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format layout_server/app.py tests/ && uv run ruff check layout_server/ tests/`

```bash
git add layout_server/app.py tests/conftest.py tests/test_app.py static/.gitkeep
git commit -m "feat: add FastAPI app skeleton with health, screens, and audio endpoints"
```

---

## Task 5: Screen image push/pull API + WebSocket frame broadcast

**Files:**
- Create: `layout_server/screen_store.py`
- Create: `layout_server/ws_manager.py`
- Create: `layout_server/screens_api.py`
- Modify: `layout_server/app.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_screens_api.py`

**Interfaces:**
- Consumes: `AppState` (Task 4).
- Produces: `layout_server.screen_store.ScreenImageStore` (`put(screen_id, content_type, data) -> int`, `get(screen_id) -> StoredImage | None`); `layout_server.ws_manager.ConnectionManager` (`connect`, `disconnect`, `broadcast(message: dict)`, `connection_count` property); `register_screen_routes(app, layout_config, store, connections)`.

- [ ] **Step 1: Write the failing test**

Update `tests/conftest.py` — add `store` and `connections` to the app so the fixture builds the full app (append after the existing `client` fixture, keep everything else unchanged):

```python
from layout_server.screen_store import ScreenImageStore
from layout_server.ws_manager import ConnectionManager
```

Add this import at the top of `tests/conftest.py` alongside the existing ones, and change the `client` fixture body's `create_app` call — no change needed there since `create_app` internally constructs its own store/connections in Task 5's implementation (see Step 3 below); the fixture itself doesn't need to change further.

Create `tests/test_screens_api.py`:

```python
import io

from PIL import Image


def _png_bytes(color=(255, 0, 0)) -> bytes:
    image = Image.new("RGB", (4, 4), color=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_push_image_unknown_screen_returns_404(client):
    response = client.post("/screens/Z/image", content=_png_bytes(), headers={"content-type": "image/png"})
    assert response.status_code == 404


def test_push_image_bad_content_type_returns_400(client):
    response = client.post("/screens/F/image", content=b"not an image", headers={"content-type": "text/plain"})
    assert response.status_code == 400


def test_push_image_undecodable_bytes_returns_400(client):
    response = client.post("/screens/F/image", content=b"not a real png", headers={"content-type": "image/png"})
    assert response.status_code == 400


def test_push_then_get_image_round_trip(client):
    push_response = client.post("/screens/F/image", content=_png_bytes(), headers={"content-type": "image/png"})
    assert push_response.status_code == 200
    assert push_response.json() == {"version": 1}

    get_response = client.get("/screens/F/image")
    assert get_response.status_code == 200
    assert get_response.content == _png_bytes()
    assert get_response.headers["content-type"] == "image/png"


def test_get_image_before_any_push_returns_404(client):
    response = client.get("/screens/B/image")
    assert response.status_code == 404


def test_push_image_broadcasts_frame_over_websocket(client):
    with client.websocket_connect("/ws") as websocket:
        client.post("/screens/F/image", content=_png_bytes(), headers={"content-type": "image/png"})
        message = websocket.receive_json()
        assert message == {"type": "frame", "screen": "F", "version": 1, "transition_ms": 500}


def test_push_image_respects_transition_ms_query_param(client):
    with client.websocket_connect("/ws") as websocket:
        client.post(
            "/screens/F/image?transition_ms=200",
            content=_png_bytes(),
            headers={"content-type": "image/png"},
        )
        message = websocket.receive_json()
        assert message["transition_ms"] == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screens_api.py -v`
Expected: FAIL — `/screens/F/image` doesn't exist yet (404 from the catch-all static mount, not the real validation logic; other assertions fail).

- [ ] **Step 3: Implement the store, connection manager, and routes**

Create `layout_server/screen_store.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StoredImage:
    content_type: str
    data: bytes
    version: int


class ScreenImageStore:
    def __init__(self) -> None:
        self._images: dict[str, StoredImage] = {}

    def put(self, screen_id: str, content_type: str, data: bytes) -> int:
        current = self._images.get(screen_id)
        version = (current.version + 1) if current else 1
        self._images[screen_id] = StoredImage(content_type=content_type, data=data, version=version)
        return version

    def get(self, screen_id: str) -> StoredImage | None:
        return self._images.get(screen_id)
```

Create `layout_server/ws_manager.py`:

```python
from __future__ import annotations

import json

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        payload = json.dumps(message)
        stale: list[WebSocket] = []
        for connection in self._connections:
            try:
                await connection.send_text(payload)
            except Exception:
                stale.append(connection)
        for connection in stale:
            self.disconnect(connection)

    @property
    def connection_count(self) -> int:
        return len(self._connections)
```

Create `layout_server/screens_api.py`:

```python
from __future__ import annotations

import io

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from PIL import Image

from .config import LayoutConfig
from .screen_store import ScreenImageStore
from .ws_manager import ConnectionManager

_VALID_CONTENT_TYPES = {"image/png", "image/jpeg"}


def register_screen_routes(
    app: FastAPI,
    layout_config: LayoutConfig,
    store: ScreenImageStore,
    connections: ConnectionManager,
) -> None:
    @app.post("/screens/{screen_id}/image")
    async def push_image(
        screen_id: str, request: Request, transition_ms: int = Query(default=500)
    ) -> dict[str, int]:
        if layout_config.screen_by_id(screen_id) is None:
            raise HTTPException(status_code=404, detail=f"Unknown screen {screen_id!r}")

        content_type = request.headers.get("content-type", "")
        if content_type not in _VALID_CONTENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported content-type {content_type!r}")

        body = await request.body()
        try:
            Image.open(io.BytesIO(body)).verify()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Image bytes did not decode") from exc

        version = store.put(screen_id, content_type, body)
        await connections.broadcast(
            {"type": "frame", "screen": screen_id, "version": version, "transition_ms": transition_ms}
        )
        return {"version": version}

    @app.get("/screens/{screen_id}/image")
    async def get_image(screen_id: str, v: int | None = Query(default=None)) -> Response:
        stored = store.get(screen_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"No image pushed for {screen_id!r} yet")
        return Response(content=stored.data, media_type=stored.content_type)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await connections.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            connections.disconnect(websocket)
```

Modify `layout_server/app.py` — add the import and wire the store/connections/routes. Change:

```python
from .audio import AudioConfig, AudioDeviceList, discover_audio_devices, load_audio_config, write_audio_devices_file
from .config import LayoutConfig, load_layout_config
```

to:

```python
from .audio import AudioConfig, AudioDeviceList, discover_audio_devices, load_audio_config, write_audio_devices_file
from .config import LayoutConfig, load_layout_config
from .screen_store import ScreenImageStore
from .screens_api import register_screen_routes
from .ws_manager import ConnectionManager
```

and, inside `create_app`, right after `app.state.layout = state`, add:

```python
    store = ScreenImageStore()
    connections = ConnectionManager()
    register_screen_routes(app, state.layout_config, store, connections)
```

placing this **before** the `@app.get("/layout-driver.js")` route and the final `app.mount("/", ...)` call (route registration order matters — explicit routes must be added before the catch-all static mount).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_screens_api.py -v`
Expected: 7 passed.

Run full suite to check nothing else broke: `uv run pytest -v`
Expected: all passed.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format layout_server/ tests/ && uv run ruff check layout_server/ tests/`

```bash
git add layout_server/screen_store.py layout_server/ws_manager.py layout_server/screens_api.py layout_server/app.py tests/test_screens_api.py
git commit -m "feat: add screen image push/pull API and websocket frame broadcast"
```

---

## Task 6: Full-wall screenshot round trip

**Files:**
- Create: `layout_server/screenshot.py`
- Modify: `layout_server/app.py`
- Test: `tests/test_screenshot.py`

**Interfaces:**
- Consumes: `ConnectionManager` (Task 5).
- Produces: `layout_server.screenshot.ScreenshotBroker` (`new_request() -> tuple[str, asyncio.Future[bytes]]`, `resolve(request_id: str, data: bytes) -> bool`, `discard(request_id: str) -> None`); `register_screenshot_routes(app, connections, broker)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_screenshot.py`:

```python
import threading


def test_screenshot_with_no_connected_client_returns_504(client):
    response = client.post("/api/screenshot")
    assert response.status_code == 504


def test_screenshot_round_trip(client):
    result: dict = {}

    def do_post() -> None:
        response = client.post("/api/screenshot")
        result["status_code"] = response.status_code
        result["content"] = response.content

    with client.websocket_connect("/ws") as websocket:
        thread = threading.Thread(target=do_post)
        thread.start()

        message = websocket.receive_json()
        assert message["type"] == "screenshot_request"
        request_id = message["request_id"]

        client.post(f"/api/screenshot-result/{request_id}", content=b"fake-png-bytes")
        thread.join(timeout=5)

    assert result["status_code"] == 200
    assert result["content"] == b"fake-png-bytes"


def test_screenshot_result_for_unknown_request_id_returns_404(client):
    response = client.post("/api/screenshot-result/does-not-exist", content=b"data")
    assert response.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screenshot.py -v`
Expected: FAIL — `/api/screenshot` doesn't exist yet.

- [ ] **Step 3: Implement `layout_server/screenshot.py`**

```python
from __future__ import annotations

import asyncio
import uuid

from fastapi import FastAPI, HTTPException, Request, Response

from .ws_manager import ConnectionManager

SCREENSHOT_TIMEOUT_SECONDS = 2.0


class ScreenshotBroker:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[bytes]] = {}

    def new_request(self) -> tuple[str, asyncio.Future[bytes]]:
        request_id = uuid.uuid4().hex
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return request_id, future

    def resolve(self, request_id: str, data: bytes) -> bool:
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(data)
        return True

    def discard(self, request_id: str) -> None:
        self._pending.pop(request_id, None)


def register_screenshot_routes(app: FastAPI, connections: ConnectionManager, broker: ScreenshotBroker) -> None:
    @app.post("/api/screenshot")
    async def take_screenshot() -> Response:
        if connections.connection_count == 0:
            raise HTTPException(status_code=504, detail="No browser client connected")

        request_id, future = broker.new_request()
        await connections.broadcast({"type": "screenshot_request", "request_id": request_id})

        try:
            data = await asyncio.wait_for(future, timeout=SCREENSHOT_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            broker.discard(request_id)
            raise HTTPException(status_code=504, detail="Screenshot request timed out") from exc

        return Response(content=data, media_type="image/png")

    @app.post("/api/screenshot-result/{request_id}")
    async def screenshot_result(request_id: str, request: Request) -> dict[str, bool]:
        data = await request.body()
        resolved = broker.resolve(request_id, data)
        if not resolved:
            raise HTTPException(status_code=404, detail="Unknown or already-resolved request_id")
        return {"ok": True}
```

Modify `layout_server/app.py` — add the import:

```python
from .screenshot import ScreenshotBroker, register_screenshot_routes
```

and, right after `register_screen_routes(app, state.layout_config, store, connections)`, add:

```python
    screenshot_broker = ScreenshotBroker()
    register_screenshot_routes(app, connections, screenshot_broker)
```

again, before the `/layout-driver.js` route and the final static mount.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_screenshot.py -v`
Expected: 3 passed.

Run full suite: `uv run pytest -v`
Expected: all passed.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format layout_server/ tests/ && uv run ruff check layout_server/ tests/`

```bash
git add layout_server/screenshot.py layout_server/app.py tests/test_screenshot.py
git commit -m "feat: add full-wall screenshot request/response round trip"
```

---

## Task 7: Server entrypoint (`main.py`) and settings resolution

**Files:**
- Create: `layout_server/main.py`
- Create: `apps/test-pattern/static/index.html`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `create_app` (Task 6); `ensure_self_signed_cert` (Task 3).
- Produces: `layout_server.main.ServerSettings` (dataclass); `resolve_settings(env: dict[str, str]) -> ServerSettings`; `main() -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_main.py`:

```python
from pathlib import Path

from layout_server.main import REPO_ROOT, resolve_settings


def test_resolve_settings_defaults():
    settings = resolve_settings({})
    assert settings.host == "0.0.0.0"
    assert settings.port == 8443
    assert settings.app_static_dir == REPO_ROOT / "apps" / "test-pattern" / "static"
    assert settings.screens_yaml == REPO_ROOT / "config" / "screens.yaml"
    assert settings.audio_yaml == REPO_ROOT / "config" / "audio.yaml"
    assert settings.runtime_dir == REPO_ROOT / "runtime"
    assert settings.cert_path == REPO_ROOT / "runtime" / "cert.pem"
    assert settings.key_path == REPO_ROOT / "runtime" / "key.pem"


def test_resolve_settings_env_overrides():
    settings = resolve_settings(
        {
            "LAYOUT_DRIVER_HOST": "127.0.0.1",
            "LAYOUT_DRIVER_PORT": "9000",
            "APP_DIR": "/tmp/custom-app",
            "LAYOUT_DRIVER_RUNTIME_DIR": "/tmp/custom-runtime",
        }
    )
    assert settings.host == "127.0.0.1"
    assert settings.port == 9000
    assert settings.app_static_dir == Path("/tmp/custom-app")
    assert settings.runtime_dir == Path("/tmp/custom-runtime")
    assert settings.cert_path == Path("/tmp/custom-runtime/cert.pem")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'layout_server.main'`.

- [ ] **Step 3: Implement `layout_server/main.py`**

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from .app import create_app
from .certs import ensure_self_signed_cert

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    app_static_dir: Path
    cert_path: Path
    key_path: Path
    runtime_dir: Path
    screens_yaml: Path
    audio_yaml: Path


def resolve_settings(env: dict[str, str]) -> ServerSettings:
    runtime_dir = Path(env.get("LAYOUT_DRIVER_RUNTIME_DIR", str(REPO_ROOT / "runtime")))
    return ServerSettings(
        host=env.get("LAYOUT_DRIVER_HOST", "0.0.0.0"),
        port=int(env.get("LAYOUT_DRIVER_PORT", "8443")),
        app_static_dir=Path(env.get("APP_DIR", str(REPO_ROOT / "apps" / "test-pattern" / "static"))),
        cert_path=Path(env.get("LAYOUT_DRIVER_SSL_CERT", str(runtime_dir / "cert.pem"))),
        key_path=Path(env.get("LAYOUT_DRIVER_SSL_KEY", str(runtime_dir / "key.pem"))),
        runtime_dir=runtime_dir,
        screens_yaml=Path(env.get("SCREENS_YAML", str(REPO_ROOT / "config" / "screens.yaml"))),
        audio_yaml=Path(env.get("AUDIO_YAML", str(REPO_ROOT / "config" / "audio.yaml"))),
    )


def main() -> None:
    settings = resolve_settings(dict(os.environ))
    ensure_self_signed_cert(settings.cert_path, settings.key_path)

    app = create_app(
        screens_yaml=settings.screens_yaml,
        audio_yaml=settings.audio_yaml,
        runtime_dir=settings.runtime_dir,
        app_static_dir=settings.app_static_dir,
        framework_static_dir=REPO_ROOT / "static",
    )

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        ssl_certfile=str(settings.cert_path),
        ssl_keyfile=str(settings.key_path),
    )


if __name__ == "__main__":
    main()
```

Create a minimal `apps/test-pattern/static/index.html` so `main()` has something real to serve by default (this gets its real content in Task 9; for now it just needs to exist and be valid HTML):

```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Layout Driver — Test Pattern</title>
  </head>
  <body>
    <p>Test pattern app placeholder — populated in Task 9.</p>
  </body>
</html>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main.py -v`
Expected: 2 passed.

- [ ] **Step 5: Manually verify the real server boots**

Run: `uv run python -m layout_server.main`
Expected: log output showing uvicorn started on `https://0.0.0.0:8443`, and `runtime/cert.pem` / `runtime/key.pem` now exist.

In another terminal: `curl -k https://localhost:8443/healthz`
Expected: `{"status":"ok"}`

`curl -k https://localhost:8443/`
Expected: the placeholder HTML from Step 3.

Stop the server (Ctrl-C).

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff format layout_server/main.py tests/test_main.py && uv run ruff check layout_server/ tests/`

```bash
git add layout_server/main.py apps/test-pattern/static/index.html tests/test_main.py
git commit -m "feat: add server entrypoint with env-based settings resolution"
```

---

## Task 8: Client geometry math (`geometry.js`, `device-match.js`)

**Files:**
- Create: `static/geometry.js`
- Create: `static/geometry.test.mjs`
- Create: `static/device-match.js`
- Create: `static/device-match.test.mjs`

**Interfaces:**
- Consumes: nothing (pure functions, no DOM).
- Produces: `computeCoverFit(sourceWidth, sourceHeight, destWidth, destHeight) -> {sx, sy, sWidth, sHeight, dx, dy, dWidth, dHeight}`; `computeCompositePlacements(screens: Array<{id, rect: {x,y,width,height}}>) -> Array<{id, dx, dy, dWidth, dHeight}>`; `matchDeviceByName(name: string, devices: Array<{deviceId, label}>) -> {deviceId, label} | null`.

- [ ] **Step 1: Write the failing tests**

Create `static/geometry.test.mjs`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { computeCoverFit, computeCompositePlacements } from "./geometry.js";

test("computeCoverFit crops a wider source for a 9:7 destination (screen F)", () => {
  const result = computeCoverFit(1920, 1080, 1800, 1400);
  assert.equal(result.sHeight, 1080);
  assert.ok(Math.abs(result.sWidth - 1080 * (1800 / 1400)) < 0.001);
  assert.ok(result.sx > 0);
  assert.equal(result.sy, 0);
  assert.deepEqual(
    { dx: result.dx, dy: result.dy, dWidth: result.dWidth, dHeight: result.dHeight },
    { dx: 0, dy: 0, dWidth: 1800, dHeight: 1400 }
  );
});

test("computeCoverFit crops a taller source for a 2:1 destination (screens B/C)", () => {
  const result = computeCoverFit(1000, 1000, 1200, 600);
  assert.equal(result.sWidth, 1000);
  assert.ok(Math.abs(result.sHeight - 500) < 0.001);
  assert.equal(result.sx, 0);
  assert.ok(result.sy > 0);
});

test("computeCoverFit crops a source for a 4:1 destination (screens D/A/E)", () => {
  const result = computeCoverFit(1600, 1600, 1600, 400);
  assert.equal(result.sWidth, 1600);
  assert.ok(Math.abs(result.sHeight - 400) < 0.001);
  assert.equal(result.sx, 0);
  assert.ok(result.sy > 0);
});

test("computeCoverFit returns full-frame params for an exact aspect match", () => {
  const result = computeCoverFit(1800, 1400, 1800, 1400);
  assert.equal(result.sx, 0);
  assert.equal(result.sy, 0);
  assert.equal(result.sWidth, 1800);
  assert.equal(result.sHeight, 1400);
});

test("computeCompositePlacements places each screen at its own rect", () => {
  const screens = [
    { id: "F", rect: { x: 220, y: 80, width: 1800, height: 1400 } },
    { id: "B", rect: { x: 2020, y: 80, width: 1200, height: 600 } },
  ];
  const placements = computeCompositePlacements(screens);
  assert.deepEqual(placements[0], { id: "F", dx: 220, dy: 80, dWidth: 1800, dHeight: 1400 });
  assert.deepEqual(placements[1], { id: "B", dx: 2020, dy: 80, dWidth: 1200, dHeight: 600 });
});
```

Create `static/device-match.test.mjs`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { matchDeviceByName } from "./device-match.js";

test("matches exact name case-insensitively", () => {
  const devices = [
    { deviceId: "1", label: "BlackHole 2ch" },
    { deviceId: "2", label: "MacBook Pro Speakers" },
  ];
  const match = matchDeviceByName("blackhole 2ch", devices);
  assert.equal(match.deviceId, "1");
});

test("matches by substring", () => {
  const devices = [{ deviceId: "1", label: "BlackHole 2ch" }];
  const match = matchDeviceByName("blackhole", devices);
  assert.equal(match.deviceId, "1");
});

test("returns null when nothing matches", () => {
  const devices = [{ deviceId: "1", label: "BlackHole 2ch" }];
  assert.equal(matchDeviceByName("nonexistent device", devices), null);
});

test("returns null for empty name", () => {
  assert.equal(matchDeviceByName("", [{ deviceId: "1", label: "x" }]), null);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test static/`
Expected: FAIL — `Cannot find module './geometry.js'` and `'./device-match.js'`.

- [ ] **Step 3: Implement `static/geometry.js`**

```js
export function computeCoverFit(sourceWidth, sourceHeight, destWidth, destHeight) {
  const sourceAspect = sourceWidth / sourceHeight;
  const destAspect = destWidth / destHeight;

  let sx, sy, sWidth, sHeight;
  if (sourceAspect > destAspect) {
    sHeight = sourceHeight;
    sWidth = sourceHeight * destAspect;
    sx = (sourceWidth - sWidth) / 2;
    sy = 0;
  } else {
    sWidth = sourceWidth;
    sHeight = sourceWidth / destAspect;
    sx = 0;
    sy = (sourceHeight - sHeight) / 2;
  }

  return { sx, sy, sWidth, sHeight, dx: 0, dy: 0, dWidth: destWidth, dHeight: destHeight };
}

export function computeCompositePlacements(screens) {
  return screens.map((screen) => ({
    id: screen.id,
    dx: screen.rect.x,
    dy: screen.rect.y,
    dWidth: screen.rect.width,
    dHeight: screen.rect.height,
  }));
}
```

- [ ] **Step 4: Implement `static/device-match.js`**

```js
export function matchDeviceByName(name, devices) {
  if (!name) {
    return null;
  }
  const lowered = name.toLowerCase();
  return devices.find((device) => device.label.toLowerCase().includes(lowered)) ?? null;
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `node --test static/`
Expected: 9 passed (5 geometry + 4 device-match).

- [ ] **Step 6: Commit**

```bash
git add static/geometry.js static/geometry.test.mjs static/device-match.js static/device-match.test.mjs
git commit -m "feat: add client-side cover-fit, compositing, and device-matching math"
```

---

## Task 9: `layout-driver.js` core (screen containers + shared WebSocket dispatcher)

**Files:**
- Create: `static/layout-driver.js`
- Modify: `apps/test-pattern/static/index.html`

**Interfaces:**
- Consumes: nothing from earlier JS tasks directly (geometry/device-match are consumed by Tasks 10–11).
- Produces (this task): `initLayoutDriver() -> Promise<LayoutDriver>` where `LayoutDriver = { layoutConfig, getScreenContainer(id) -> {element, width, height, x, y}, onMessage(handler: (message) => void) }`. Later tasks (10, 11) add `enableImageMode(driver)`, `enableScreenshotResponder(driver)`, `routeAudioElement(el)` to this same file.

- [ ] **Step 1: Implement `static/layout-driver.js`**

There's no meaningful way to unit test DOM construction without adding a DOM-emulation dependency, which the tech stack deliberately avoids (spec §9: "no bundler/build step ... kept dependency-free"). This task is verified manually in Step 3 against a real browser instead.

```js
const CANVAS_WIDTH = 3840;
const CANVAS_HEIGHT = 2160;

async function fetchScreens() {
  const response = await fetch("/api/screens");
  if (!response.ok) {
    throw new Error(`Failed to fetch /api/screens: ${response.status}`);
  }
  return response.json();
}

function buildRoot(layoutConfig) {
  document.body.style.margin = "0";
  document.body.style.overflow = "hidden";
  document.body.style.background = "black";

  const root = document.createElement("div");
  root.id = "layout-driver-root";
  root.style.position = "absolute";
  root.style.top = "0";
  root.style.left = "0";
  root.style.width = `${CANVAS_WIDTH}px`;
  root.style.height = `${CANVAS_HEIGHT}px`;
  root.style.background = "black";
  root.style.transformOrigin = "top left";
  document.body.appendChild(root);

  const containers = new Map();
  for (const screen of layoutConfig.screens) {
    const container = document.createElement("div");
    container.id = `screen-${screen.id}`;
    container.style.position = "absolute";
    container.style.left = `${screen.rect.x}px`;
    container.style.top = `${screen.rect.y}px`;
    container.style.width = `${screen.rect.width}px`;
    container.style.height = `${screen.rect.height}px`;
    container.style.overflow = "hidden";
    root.appendChild(container);
    containers.set(screen.id, {
      element: container,
      width: screen.rect.width,
      height: screen.rect.height,
      x: screen.rect.x,
      y: screen.rect.y,
    });
  }

  function rescale() {
    const scale = Math.min(window.innerWidth / CANVAS_WIDTH, window.innerHeight / CANVAS_HEIGHT);
    root.style.transform = `scale(${scale})`;
  }
  window.addEventListener("resize", rescale);
  rescale();

  return containers;
}

function connectWebSocket(handlers) {
  function connect() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
    ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      for (const handler of handlers) {
        handler(message);
      }
    });
    ws.addEventListener("open", () => {
      for (const handler of handlers) {
        handler({ type: "_connected" });
      }
    });
    ws.addEventListener("close", () => {
      setTimeout(connect, 1000);
    });
    return ws;
  }
  return connect();
}

export async function initLayoutDriver() {
  const layoutConfig = await fetchScreens();
  const containers = buildRoot(layoutConfig);
  const messageHandlers = [];

  const driver = {
    layoutConfig,
    getScreenContainer(id) {
      const container = containers.get(id);
      if (!container) {
        throw new Error(`Unknown screen id: ${id}`);
      }
      return container;
    },
    onMessage(handler) {
      messageHandlers.push(handler);
    },
  };

  connectWebSocket(messageHandlers);

  return driver;
}
```

- [ ] **Step 2: Wire up the test-pattern app**

Replace the contents of `apps/test-pattern/static/index.html` (from Task 7's placeholder):

```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Layout Driver — Test Pattern</title>
  </head>
  <body>
    <script type="module">
      import { initLayoutDriver } from "/layout-driver.js";

      const COLORS = {
        F: "#e63946",
        B: "#2a9d8f",
        C: "#e9c46a",
        D: "#264653",
        A: "#f4a261",
        E: "#8338ec",
      };

      const driver = await initLayoutDriver();
      window.LayoutDriver = driver;

      for (const screen of driver.layoutConfig.screens) {
        const container = driver.getScreenContainer(screen.id);
        const canvas = document.createElement("canvas");
        canvas.width = container.width;
        canvas.height = container.height;
        container.element.appendChild(canvas);
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = COLORS[screen.id] ?? "#888888";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = "black";
        ctx.font = "48px sans-serif";
        ctx.fillText(`${screen.name} (${screen.rect.width}x${screen.rect.height})`, 20, 60);
      }
    </script>
  </body>
</html>
```

- [ ] **Step 3: Manually verify in a browser**

Run: `uv run python -m layout_server.main`

Open `https://localhost:8443/` in Chrome (accept the self-signed certificate warning).

Expected: 6 colored rectangles, each labeled with its screen name and pixel size, positioned exactly per the spec §2 table (screen F large in the upper-left; B and C stacked upper-right; D and A stacked lower-right, wider than B/C; E lower-left, indented one module from F's left edge). The overall colored region should be centered with roughly equal black margins on all sides (220px left/right, 80px top/bottom, scaled to fit the window).

Open the browser devtools console and run:

```js
window.LayoutDriver.layoutConfig.screens.length
```

Expected: `6`

```js
window.LayoutDriver.getScreenContainer("F")
```

Expected: an object with `width: 1800, height: 1400, x: 220, y: 80`.

Stop the server (Ctrl-C).

- [ ] **Step 4: Commit**

```bash
git add static/layout-driver.js apps/test-pattern/static/index.html
git commit -m "feat: add layout-driver.js core (screen containers, shared websocket dispatcher)"
```

---

## Task 10: `layout-driver.js` image mode (push-driven apps)

**Files:**
- Modify: `static/layout-driver.js`

**Interfaces:**
- Consumes: `computeCoverFit` (Task 8); `initLayoutDriver`'s `driver.onMessage`, `driver.getScreenContainer` (Task 9).
- Produces: `enableImageMode(driver) -> {canvases: Map<string, HTMLCanvasElement>}` appended to `static/layout-driver.js`. Fetches `/screens/{id}/image?v=...` and cover-fit-draws it with a crossfade.

- [ ] **Step 1: Implement `enableImageMode`**

Add to the top of `static/layout-driver.js` (alongside the existing imports — there are none yet, so this becomes the first import line):

```js
import { computeCoverFit } from "./geometry.js";
```

Append to the end of `static/layout-driver.js`:

```js
function drawCoverFit(ctx, image, canvasWidth, canvasHeight) {
  const fit = computeCoverFit(image.width, image.height, canvasWidth, canvasHeight);
  ctx.drawImage(image, fit.sx, fit.sy, fit.sWidth, fit.sHeight, fit.dx, fit.dy, fit.dWidth, fit.dHeight);
}

async function loadScreenImage(screenId, version) {
  const response = await fetch(`/screens/${screenId}/image?v=${version}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch image for ${screenId}: ${response.status}`);
  }
  const blob = await response.blob();
  return createImageBitmap(blob);
}

export function enableImageMode(driver) {
  const layers = new Map();

  for (const screen of driver.layoutConfig.screens) {
    const container = driver.getScreenContainer(screen.id);
    container.element.style.position = "relative";

    const canvasA = document.createElement("canvas");
    const canvasB = document.createElement("canvas");
    for (const canvas of [canvasA, canvasB]) {
      canvas.width = container.width;
      canvas.height = container.height;
      canvas.style.position = "absolute";
      canvas.style.top = "0";
      canvas.style.left = "0";
      canvas.style.transition = "opacity 300ms linear";
      container.element.appendChild(canvas);
    }
    canvasB.style.opacity = "0";

    layers.set(screen.id, { canvases: [canvasA, canvasB], activeIndex: 0 });
  }

  async function applyFrame(screenId, version, transitionMs) {
    const layer = layers.get(screenId);
    if (!layer) {
      return;
    }
    const image = await loadScreenImage(screenId, version);
    const nextIndex = 1 - layer.activeIndex;
    const nextCanvas = layer.canvases[nextIndex];
    const currentCanvas = layer.canvases[layer.activeIndex];

    drawCoverFit(nextCanvas.getContext("2d"), image, nextCanvas.width, nextCanvas.height);
    nextCanvas.style.transition = `opacity ${transitionMs}ms linear`;
    currentCanvas.style.transition = `opacity ${transitionMs}ms linear`;
    nextCanvas.style.opacity = "1";
    currentCanvas.style.opacity = "0";
    layer.activeIndex = nextIndex;
  }

  async function resync() {
    for (const screenId of layers.keys()) {
      try {
        await applyFrame(screenId, Date.now(), 0);
      } catch {
        // No image has been pushed for this screen yet — leave it blank.
      }
    }
  }

  driver.onMessage((message) => {
    if (message.type === "frame") {
      applyFrame(message.screen, message.version, message.transition_ms);
    } else if (message.type === "_connected") {
      resync();
    }
  });

  return { canvases: new Map([...layers].map(([id, layer]) => [id, layer.canvases[layer.activeIndex]])) };
}
```

- [ ] **Step 2: Manually verify in a browser**

Temporarily point the test-pattern app's bootstrap at image mode instead of the static-rectangle drawing, to verify the push path end to end. Create a throwaway file `apps/test-pattern/static/image-mode-check.html` (not wired into `run.sh`, just for this manual check — delete it at the end of this step):

```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Image Mode Check</title>
  </head>
  <body>
    <script type="module">
      import { initLayoutDriver, enableImageMode } from "/layout-driver.js";
      const driver = await initLayoutDriver();
      enableImageMode(driver);
      window.LayoutDriver = driver;
    </script>
  </body>
</html>
```

Run: `APP_DIR=$(pwd)/apps/test-pattern/static uv run python -m layout_server.main`

Open `https://localhost:8443/image-mode-check.html` in a browser.

In another terminal, push a solid-color test image:

```bash
uv run python -c "
import io
from PIL import Image
buf = io.BytesIO()
Image.new('RGB', (400, 400), color=(0, 200, 0)).save(buf, format='PNG')
import urllib.request, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
req = urllib.request.Request('https://localhost:8443/screens/F/image', data=buf.getvalue(), headers={'Content-Type': 'image/png'}, method='POST')
print(urllib.request.urlopen(req, context=ctx).read())
"
```

Expected: within `transition_ms` (default 500ms), screen F's area crossfades in a green square, cropped to fill F's 1800×1400 region (cover-fit — center-cropped square, not stretched).

Reload the page — expected: the green image reappears immediately without needing another push (this exercises `resync()` on the fresh WS connection).

Delete the throwaway file: `rm apps/test-pattern/static/image-mode-check.html`

- [ ] **Step 3: Commit**

```bash
git add static/layout-driver.js
git commit -m "feat: add layout-driver.js image mode with cover-fit crossfade"
```

---

## Task 11: `layout-driver.js` screenshot responder and audio routing

**Files:**
- Modify: `static/layout-driver.js`

**Interfaces:**
- Consumes: `computeCompositePlacements` (Task 8); `matchDeviceByName` (Task 8); `driver.onMessage`, `driver.getScreenContainer`, `driver.layoutConfig` (Task 9).
- Produces: `enableScreenshotResponder(driver) -> {composite}` and `routeAudioElement(el) -> Promise<void>` appended to `static/layout-driver.js`.

- [ ] **Step 1: Implement both functions**

Add to the top imports of `static/layout-driver.js`:

```js
import { computeCompositePlacements } from "./geometry.js";
import { matchDeviceByName } from "./device-match.js";
```

(combine with the existing `import { computeCoverFit } from "./geometry.js";` line from Task 10 into one import: `import { computeCoverFit, computeCompositePlacements } from "./geometry.js";`)

Append to the end of `static/layout-driver.js`:

```js
export function enableScreenshotResponder(driver) {
  function findCanvas(screenId) {
    const container = driver.getScreenContainer(screenId);
    return container.element.querySelector("canvas");
  }

  async function composite() {
    const offscreen = document.createElement("canvas");
    offscreen.width = 3840;
    offscreen.height = 2160;
    const ctx = offscreen.getContext("2d");
    ctx.fillStyle = "black";
    ctx.fillRect(0, 0, offscreen.width, offscreen.height);

    for (const placement of computeCompositePlacements(driver.layoutConfig.screens)) {
      const canvas = findCanvas(placement.id);
      if (!canvas) {
        continue;
      }
      ctx.drawImage(canvas, placement.dx, placement.dy, placement.dWidth, placement.dHeight);
    }

    return new Promise((resolve) => offscreen.toBlob(resolve, "image/png"));
  }

  driver.onMessage(async (message) => {
    if (message.type !== "screenshot_request") {
      return;
    }
    const blob = await composite();
    await fetch(`/api/screenshot-result/${message.request_id}`, { method: "POST", body: blob });
  });

  return { composite };
}

export async function routeAudioElement(el) {
  const response = await fetch("/api/audio-config");
  const config = await response.json();
  if (!config.enabled || !config.output_device) {
    return;
  }

  await navigator.mediaDevices.getUserMedia({ audio: true }).catch(() => null);
  const devices = await navigator.mediaDevices.enumerateDevices();
  const outputs = devices
    .filter((device) => device.kind === "audiooutput")
    .map((device) => ({ deviceId: device.deviceId, label: device.label }));

  const match = matchDeviceByName(config.output_device, outputs);
  if (!match) {
    console.warn(`Audio output device not found: ${config.output_device}`);
    return;
  }
  if (typeof el.setSinkId === "function") {
    await el.setSinkId(match.deviceId);
  }
}
```

- [ ] **Step 2: Manually verify the screenshot round trip**

Run: `uv run python -m layout_server.main` (serving the real test-pattern app from Task 9, which draws canvases directly — no image mode needed).

Create a throwaway `apps/test-pattern/static/screenshot-check.html`:

```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Screenshot Check</title>
  </head>
  <body>
    <script type="module">
      import { initLayoutDriver, enableScreenshotResponder } from "/layout-driver.js";
      const driver = await initLayoutDriver();
      window.LayoutDriver = driver;

      const COLORS = { F: "#e63946", B: "#2a9d8f", C: "#e9c46a", D: "#264653", A: "#f4a261", E: "#8338ec" };
      for (const screen of driver.layoutConfig.screens) {
        const container = driver.getScreenContainer(screen.id);
        const canvas = document.createElement("canvas");
        canvas.width = container.width;
        canvas.height = container.height;
        container.element.appendChild(canvas);
        canvas.getContext("2d").fillStyle = COLORS[screen.id];
        canvas.getContext("2d").fillRect(0, 0, canvas.width, canvas.height);
      }

      enableScreenshotResponder(driver);
    </script>
  </body>
</html>
```

Open `https://localhost:8443/screenshot-check.html`.

In another terminal:

```bash
curl -k -o /tmp/wall-screenshot.png -X POST https://localhost:8443/api/screenshot
file /tmp/wall-screenshot.png
```

Expected: `/tmp/wall-screenshot.png: PNG image data, 3840 x 2160`. Open the file — expected: 6 colored rectangles positioned exactly as in the Task 9 manual check, composited into one image.

Delete the throwaway file: `rm apps/test-pattern/static/screenshot-check.html`

- [ ] **Step 3: Commit**

```bash
git add static/layout-driver.js
git commit -m "feat: add layout-driver.js screenshot responder and audio output routing"
```

---

## Task 12: NDI broadcaster config and health-check launcher

**Files:**
- Create: `config/broadcaster.yaml`
- Create: `ndi_broadcaster/__init__.py`
- Create: `ndi_broadcaster/config.py`
- Create: `ndi_broadcaster/launcher.py`
- Test: `tests/test_broadcaster_config.py`
- Test: `tests/test_launcher.py`

**Interfaces:**
- Consumes: nothing from `layout_server`.
- Produces: `ndi_broadcaster.config.BroadcasterConfig` (Pydantic model), `load_broadcaster_config(path: Path) -> BroadcasterConfig`; `ndi_broadcaster.launcher.wait_for_healthy(url: str, timeout_seconds: float, poll_interval_seconds: float = 0.5) -> None`, `HealthCheckTimeoutError(RuntimeError)`.

- [ ] **Step 1: Create the broadcaster config fixture**

Create `config/broadcaster.yaml`:

```yaml
target_url: "https://localhost:8443/"
capture_backend: "cdp"
ndi_source_name: "Layout Driver"
width: 3840
height: 2160
fps: 30
healthz_timeout_seconds: 30.0
```

- [ ] **Step 2: Write the failing tests**

Create `ndi_broadcaster/__init__.py` (empty file).

Create `tests/test_broadcaster_config.py`:

```python
from pathlib import Path

from ndi_broadcaster.config import BroadcasterConfig, load_broadcaster_config

BROADCASTER_YAML = Path(__file__).resolve().parent.parent / "config" / "broadcaster.yaml"


def test_load_broadcaster_config():
    config = load_broadcaster_config(BROADCASTER_YAML)
    assert config == BroadcasterConfig(
        target_url="https://localhost:8443/",
        capture_backend="cdp",
        ndi_source_name="Layout Driver",
        width=3840,
        height=2160,
        fps=30,
        healthz_timeout_seconds=30.0,
    )


def test_broadcaster_config_defaults():
    config = BroadcasterConfig()
    assert config.capture_backend == "cdp"
    assert config.width == 3840
    assert config.height == 2160
    assert config.fps == 30
```

Create `tests/test_launcher.py`:

```python
import http.server
import socketserver
import threading

import pytest

from ndi_broadcaster.launcher import HealthCheckTimeoutError, wait_for_healthy


class _HealthyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def test_wait_for_healthy_succeeds():
    with socketserver.TCPServer(("127.0.0.1", 0), _HealthyHandler) as server:
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            wait_for_healthy(f"http://127.0.0.1:{port}/healthz", timeout_seconds=5.0)
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_wait_for_healthy_times_out():
    with pytest.raises(HealthCheckTimeoutError):
        wait_for_healthy("http://127.0.0.1:1/healthz", timeout_seconds=1.0, poll_interval_seconds=0.2)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_broadcaster_config.py tests/test_launcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ndi_broadcaster.config'` / `'ndi_broadcaster.launcher'`.

- [ ] **Step 4: Implement `ndi_broadcaster/config.py`**

```python
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class BroadcasterConfig(BaseModel):
    target_url: str = "https://localhost:8443/"
    capture_backend: str = "cdp"
    ndi_source_name: str = "Layout Driver"
    width: int = 3840
    height: int = 2160
    fps: int = 30
    healthz_timeout_seconds: float = 30.0


def load_broadcaster_config(path: Path) -> BroadcasterConfig:
    raw = yaml.safe_load(path.read_text())
    return BroadcasterConfig(**raw)
```

- [ ] **Step 5: Implement `ndi_broadcaster/launcher.py`**

```python
from __future__ import annotations

import time

import httpx


class HealthCheckTimeoutError(RuntimeError):
    pass


def wait_for_healthy(url: str, timeout_seconds: float, poll_interval_seconds: float = 0.5) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2.0, verify=False)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(poll_interval_seconds)
    raise HealthCheckTimeoutError(f"{url} did not become healthy within {timeout_seconds}s") from last_error
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_broadcaster_config.py tests/test_launcher.py -v`
Expected: 4 passed.

- [ ] **Step 7: Lint and commit**

Run: `uv run ruff format ndi_broadcaster/ tests/ && uv run ruff check ndi_broadcaster/ tests/`

```bash
git add config/broadcaster.yaml ndi_broadcaster/__init__.py ndi_broadcaster/config.py ndi_broadcaster/launcher.py tests/test_broadcaster_config.py tests/test_launcher.py
git commit -m "feat: add NDI broadcaster config and healthz-polling launcher"
```

---

## Task 13: CDP capture, NDI video sender, and Playwright orchestration

**Files:**
- Create: `ndi_broadcaster/capture_cdp.py`
- Create: `ndi_broadcaster/ndi_sender.py`
- Modify: `ndi_broadcaster/launcher.py`
- Test: `tests/test_capture_cdp.py`

**Interfaces:**
- Consumes: `BroadcasterConfig`, `wait_for_healthy` (Task 12).
- Produces: `ndi_broadcaster.capture_cdp.decode_screencast_frame(base64_data: str) -> numpy.ndarray`; `ndi_broadcaster.ndi_sender.VideoSender` (`__init__(ndi_source_name, width, height, fps)`, `send(frame: numpy.ndarray) -> None`, `close() -> None`); `ndi_broadcaster.launcher.run() -> None` (the real broadcaster entrypoint).

**Important — verify the third-party API before running this task's manual step:** `cyndilib`'s exact method names for setting resolution/frame-rate/FourCC on `VideoSendFrame` may differ from what's written below, since this plan is written against the API surface described in prior research rather than the installed package's live docs. Before the manual verification step, run:

```bash
uv run python -c "from cyndilib.video_frame import VideoSendFrame; help(VideoSendFrame)"
uv run python -c "from cyndilib.sender import Sender; help(Sender)"
```

and adjust the method calls in `ndi_broadcaster/ndi_sender.py` (Step 3 below) to match what's actually installed, before attempting Step 5's manual NDI verification. The `decode_screencast_frame` function and its test (Steps 1–2) don't depend on `cyndilib` at all and need no adjustment.

- [ ] **Step 1: Write the failing test for frame decoding**

Create `tests/test_capture_cdp.py`:

```python
import base64
import io

from PIL import Image

from ndi_broadcaster.capture_cdp import decode_screencast_frame


def test_decode_screencast_frame_roundtrip():
    original = Image.new("RGB", (16, 8), color=(10, 20, 30))
    buffer = io.BytesIO()
    original.save(buffer, format="JPEG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

    decoded = decode_screencast_frame(encoded)

    assert decoded.shape == (8, 16, 4)
    assert decoded.dtype.name == "uint8"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_capture_cdp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ndi_broadcaster.capture_cdp'`.

- [ ] **Step 3: Implement `ndi_broadcaster/capture_cdp.py` and `ndi_broadcaster/ndi_sender.py`**

Create `ndi_broadcaster/capture_cdp.py`:

```python
from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image


def decode_screencast_frame(base64_data: str) -> np.ndarray:
    raw = base64.b64decode(base64_data)
    image = Image.open(io.BytesIO(raw)).convert("RGBA")
    return np.array(image)
```

Create `ndi_broadcaster/ndi_sender.py`:

```python
from __future__ import annotations

from fractions import Fraction

import numpy as np
from cyndilib.sender import Sender
from cyndilib.video_frame import VideoSendFrame
from cyndilib.wrapper.ndi_structs import FourCC


class VideoSender:
    def __init__(self, ndi_source_name: str, width: int, height: int, fps: int) -> None:
        self._sender = Sender(ndi_source_name)
        self._video_frame = VideoSendFrame()
        self._video_frame.set_resolution(width, height)
        self._video_frame.set_frame_rate(Fraction(fps, 1))
        self._video_frame.set_fourcc(FourCC.RGBA)
        self._sender.set_video_frame(self._video_frame)
        self._sender.open()

    def send(self, frame: np.ndarray) -> None:
        self._video_frame.write_data(frame.tobytes())
        self._sender.send_video_async()

    def close(self) -> None:
        self._sender.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_capture_cdp.py -v`
Expected: 1 passed. (This test doesn't import `ndi_sender.py`, so it passes regardless of whether `cyndilib`'s API needed adjusting.)

- [ ] **Step 5: Implement and manually verify the full capture/send loop**

Modify `ndi_broadcaster/launcher.py` — add imports at the top:

```python
import asyncio
import threading

from playwright.async_api import async_playwright

from .capture_cdp import decode_screencast_frame
from .config import BroadcasterConfig, load_broadcaster_config
from .ndi_sender import VideoSender
```

Append to `ndi_broadcaster/launcher.py`:

```python
async def _capture_loop(config: BroadcasterConfig, sender: VideoSender, stop_event: threading.Event) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False, args=["--kiosk"])
        context = await browser.new_context(
            viewport={"width": config.width, "height": config.height},
            ignore_https_errors=True,
            permissions=["microphone"],
        )
        page = await context.new_page()
        await page.goto(config.target_url)

        client = await context.new_cdp_session(page)
        await client.send(
            "Page.startScreencast", {"format": "jpeg", "quality": 80, "maxWidth": config.width, "maxHeight": config.height}
        )

        def on_frame(params: dict) -> None:
            frame = decode_screencast_frame(params["data"])
            sender.send(frame)
            asyncio.create_task(client.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))

        client.on("Page.screencastFrame", on_frame)

        while not stop_event.is_set():
            await asyncio.sleep(0.1)

        await browser.close()


def run(config_path: str = "config/broadcaster.yaml") -> None:
    from pathlib import Path

    config = load_broadcaster_config(Path(config_path))
    wait_for_healthy(f"{config.target_url.rstrip('/')}/healthz", timeout_seconds=config.healthz_timeout_seconds)

    sender = VideoSender(config.ndi_source_name, config.width, config.height, config.fps)
    stop_event = threading.Event()
    try:
        asyncio.run(_capture_loop(config, sender, stop_event))
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        sender.close()


if __name__ == "__main__":
    run()
```

**Manually verify** (requires a real NDI receiver on the network — e.g. the free NDI Video Monitor tool, or OBS with the NDI plugin — to confirm the stream actually appears; this cannot be scripted):

1. Start the layout server: `uv run python -m layout_server.main`
2. In another terminal, start the broadcaster: `uv run python -m ndi_broadcaster.launcher`
3. Expected: a headed Chrome window opens in kiosk mode showing the test-pattern page.
4. Open an NDI receiver on the same machine/network and look for a source named "Layout Driver".
5. Expected: the receiver shows the same 6-rectangle test pattern at 3840×2160, updating at roughly 30fps.
6. Stop both processes (Ctrl-C in each terminal).

If step 5 doesn't show a picture, re-check the `help()` output from the note above this task against `ndi_sender.py`'s method calls — this is the most likely place a version mismatch would surface.

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff format ndi_broadcaster/ tests/ && uv run ruff check ndi_broadcaster/ tests/`

```bash
git add ndi_broadcaster/capture_cdp.py ndi_broadcaster/ndi_sender.py ndi_broadcaster/launcher.py tests/test_capture_cdp.py
git commit -m "feat: add CDP capture, NDI video sender, and Playwright capture loop"
```

---

## Task 14: Loopback audio capture, ScreenCaptureKit backend, and `run.sh`

**Files:**
- Create: `ndi_broadcaster/audio_capture.py`
- Create: `ndi_broadcaster/capture_sck.py`
- Modify: `ndi_broadcaster/launcher.py`
- Create: `run.sh`
- Test: `tests/test_audio_capture.py`

**Interfaces:**
- Consumes: `AudioConfig`, `discover_audio_devices`, `match_device_by_name` (Task 2, reused directly — not reimplemented); `BroadcasterConfig` (Task 12); `VideoSender` (Task 13).
- Produces: `ndi_broadcaster.audio_capture.resolve_input_device(audio_config, devices) -> AudioDevice | None`; `ndi_broadcaster.audio_capture.AudioSender` (thin `cyndilib` `AudioSendFrame` wrapper, mirrors `VideoSender`'s shape); `ndi_broadcaster.capture_sck.SckCapture` (macOS-only, opt-in).

- [ ] **Step 1: Write the failing test**

Create `tests/test_audio_capture.py`:

```python
from layout_server.audio import AudioConfig, AudioDevice
from ndi_broadcaster.audio_capture import resolve_input_device


def test_resolve_input_device_finds_configured_device():
    config = AudioConfig(enabled=True, input_device="BlackHole 2ch", output_device="BlackHole 2ch")
    devices = [
        AudioDevice(index=2, name="BlackHole 2ch", max_input_channels=2),
        AudioDevice(index=0, name="MacBook Pro Microphone", max_input_channels=1),
    ]
    device = resolve_input_device(config, devices)
    assert device is not None
    assert device.index == 2


def test_resolve_input_device_returns_none_when_disabled():
    config = AudioConfig(enabled=False, input_device="BlackHole 2ch", output_device="BlackHole 2ch")
    devices = [AudioDevice(index=2, name="BlackHole 2ch", max_input_channels=2)]
    assert resolve_input_device(config, devices) is None


def test_resolve_input_device_returns_none_when_not_found():
    config = AudioConfig(enabled=True, input_device="Nonexistent Device", output_device="BlackHole 2ch")
    devices = [AudioDevice(index=2, name="BlackHole 2ch", max_input_channels=2)]
    assert resolve_input_device(config, devices) is None
```

This deliberately reuses `layout_server.audio.AudioDevice`/`AudioConfig`/`match_device_by_name` rather than duplicating them in `ndi_broadcaster` — both processes read the same `config/audio.yaml` shape, so the type should be shared, not re-defined.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_audio_capture.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ndi_broadcaster.audio_capture'`.

- [ ] **Step 3: Implement `ndi_broadcaster/audio_capture.py`**

**Verify before the manual step below:** as with `ndi_sender.py` in Task 13, confirm `cyndilib`'s `AudioSendFrame` API matches what's written here — run `uv run python -c "from cyndilib.audio_frame import AudioSendFrame; help(AudioSendFrame)"` and adjust if needed.

```python
from __future__ import annotations

import sounddevice as sd
from cyndilib.audio_frame import AudioSendFrame
from cyndilib.sender import Sender

from layout_server.audio import AudioConfig, AudioDevice, match_device_by_name


def resolve_input_device(config: AudioConfig, devices: list[AudioDevice]) -> AudioDevice | None:
    if not config.enabled:
        return None
    return match_device_by_name(config.input_device, devices)


class AudioSender:
    def __init__(self, sender: Sender, device: AudioDevice, sample_rate: int = 48000, channels: int = 2) -> None:
        self._audio_frame = AudioSendFrame()
        self._audio_frame.set_sample_rate(sample_rate)
        self._audio_frame.set_num_channels(channels)
        sender.set_audio_frame(self._audio_frame)

        self._stream = sd.InputStream(
            device=device.index,
            channels=channels,
            samplerate=sample_rate,
            callback=self._on_audio,
        )

    def _on_audio(self, indata, frames, time_info, status) -> None:
        self._audio_frame.write_data(indata.tobytes())

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_audio_capture.py -v`
Expected: 3 passed. (These test `resolve_input_device` only, which doesn't touch `sounddevice`/`cyndilib` — no hardware needed.)

- [ ] **Step 5: Implement the ScreenCaptureKit backend (opt-in, macOS-only)**

Create `ndi_broadcaster/capture_sck.py`:

```python
from __future__ import annotations

"""
macOS-only capture backend using ScreenCaptureKit via PyObjC, matching a
window by title substring. Opt-in (config/broadcaster.yaml: capture_backend: sck) —
requires Screen Recording permission and a headed display. There is no way to
unit test this without a real macOS window and granted permission; verify
manually per the steps below.
"""

import numpy as np


class ScreenCaptureKitUnavailableError(RuntimeError):
    pass


class SckCapture:
    def __init__(self, window_title_hint: str) -> None:
        try:
            import Quartz
            import ScreenCaptureKit
        except ImportError as exc:
            raise ScreenCaptureKitUnavailableError(
                "ScreenCaptureKit backend requires macOS + pyobjc-framework-ScreenCaptureKit"
            ) from exc

        self._quartz = Quartz
        self._sck = ScreenCaptureKit
        self._window_title_hint = window_title_hint

    def latest_frame(self) -> np.ndarray:
        raise NotImplementedError(
            "Wire up SCStream/SCStreamOutput per karaoke-test's sck_capture.py, matching "
            f"a window whose title contains {self._window_title_hint!r}, and convert the "
            "delivered CVPixelBuffer (BGRA) to an RGBA numpy array here."
        )
```

This is intentionally a stub with a clear extension point rather than a guessed-at PyObjC implementation — the exact `SCStream`/`SCStreamOutput` delegate wiring is substantial platform-specific code that needs to be written and iterated on against a real macOS machine with Screen Recording permission granted, which isn't available in this planning context. Add a task to a follow-up plan when SCK support is actually needed; CDP (Task 13) is the default and fully implemented.

- [ ] **Step 6: Wire the audio path into the launcher**

Modify `ndi_broadcaster/launcher.py` — add imports:

```python
from pathlib import Path

from layout_server.audio import discover_audio_devices, load_audio_config
from .audio_capture import AudioSender, resolve_input_device
```

Modify the `run()` function to construct the sender with audio when enabled. Replace:

```python
def run(config_path: str = "config/broadcaster.yaml") -> None:
    from pathlib import Path

    config = load_broadcaster_config(Path(config_path))
    wait_for_healthy(f"{config.target_url.rstrip('/')}/healthz", timeout_seconds=config.healthz_timeout_seconds)

    sender = VideoSender(config.ndi_source_name, config.width, config.height, config.fps)
    stop_event = threading.Event()
    try:
        asyncio.run(_capture_loop(config, sender, stop_event))
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        sender.close()
```

with:

```python
def run(
    config_path: str = "config/broadcaster.yaml",
    audio_config_path: str = "config/audio.yaml",
) -> None:
    config = load_broadcaster_config(Path(config_path))
    wait_for_healthy(f"{config.target_url.rstrip('/')}/healthz", timeout_seconds=config.healthz_timeout_seconds)

    sender = VideoSender(config.ndi_source_name, config.width, config.height, config.fps)

    audio_sender: AudioSender | None = None
    audio_config = load_audio_config(Path(audio_config_path))
    input_device = resolve_input_device(audio_config, discover_audio_devices().inputs)
    if audio_config.enabled and input_device is not None:
        audio_sender = AudioSender(sender._sender, input_device)
        audio_sender.start()
    elif audio_config.enabled:
        print(f"Audio input device not found: {audio_config.input_device!r} — continuing without audio")

    stop_event = threading.Event()
    try:
        asyncio.run(_capture_loop(config, sender, stop_event))
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        if audio_sender is not None:
            audio_sender.stop()
        sender.close()
```

- [ ] **Step 7: Create `run.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

export APP_DIR="${APP_DIR:-$REPO_ROOT/apps/test-pattern/static}"
export LAYOUT_DRIVER_HOST="${LAYOUT_DRIVER_HOST:-0.0.0.0}"
export LAYOUT_DRIVER_PORT="${LAYOUT_DRIVER_PORT:-8443}"

uv run python -m layout_server.main &
SERVER_PID=$!

BROADCASTER_PID=""
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  if [[ -n "$BROADCASTER_PID" ]]; then
    kill "$BROADCASTER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

HEALTHZ_HOST="$LAYOUT_DRIVER_HOST"
if [[ "$HEALTHZ_HOST" == "0.0.0.0" ]]; then
  HEALTHZ_HOST="localhost"
fi

uv run python -c "
from ndi_broadcaster.launcher import wait_for_healthy
wait_for_healthy('https://${HEALTHZ_HOST}:${LAYOUT_DRIVER_PORT}/healthz', timeout_seconds=30.0)
"

uv run python -m ndi_broadcaster.launcher &
BROADCASTER_PID=$!

wait "$SERVER_PID" "$BROADCASTER_PID"
```

Make it executable: `chmod +x run.sh`

- [ ] **Step 8: Manually verify the full pipeline via `run.sh`**

Run: `./run.sh`

Expected: layout server starts, healthz check succeeds, headed Chrome opens in kiosk mode showing the 6-rectangle test pattern, and (if you have an NDI receiver open) the same picture appears over NDI as "Layout Driver". If a loopback device (e.g. BlackHole) is installed and named to match `config/audio.yaml`, audio capture starts silently (no audio is actually playing yet since the test-pattern app doesn't produce sound — this just confirms it doesn't crash on startup with `enabled: true`).

Press Ctrl-C: expected both processes exit cleanly (the `trap cleanup EXIT` kills both).

If no BlackHole-like device is installed, set `enabled: false` in `config/audio.yaml` first and expect the log line `Audio input device not found... — continuing without audio` to not even print (since it's disabled, `resolve_input_device` returns `None` without a warning path — confirm the process still starts fine either way).

- [ ] **Step 9: Run the full automated test suite one more time**

Run: `uv run pytest -v` and `node --test static/`
Expected: all Python and JS tests pass.

- [ ] **Step 10: Lint and commit**

Run: `uv run ruff format ndi_broadcaster/ tests/ && uv run ruff check ndi_broadcaster/ tests/`

```bash
git add ndi_broadcaster/audio_capture.py ndi_broadcaster/capture_sck.py ndi_broadcaster/launcher.py run.sh tests/test_audio_capture.py
git commit -m "feat: add loopback audio capture, SCK backend stub, and run.sh"
```

---

## Self-Review Notes

- **Spec coverage:** §2 (geometry) → Task 1; §3.1 (server endpoints) → Tasks 4–7; §3.2 (client core/image mode) → Tasks 9–10; §3.2a (screenshot) → Tasks 6, 11; §3.3 (audio) → Tasks 2, 11, 14; §3.4 (broadcaster) → Tasks 12–14; §5 (repo structure/app isolation) → all tasks respect `apps/` vs framework separation; §6 (error handling) → covered inline in each relevant task's tests (404/400/504 cases); §7 (testing) → cover-fit (Task 8), device matching (Tasks 2, 8), compositing placement (Task 8), rect/overlap (Task 1); §9 (tech stack) → `uv`/`ruff`/Pydantic v2/no-bundler JS reflected throughout.
- **Known gap flagged, not hidden:** the ScreenCaptureKit backend (Task 14, Step 5) is a stub with a clear extension point rather than a full implementation — this is called out explicitly in that step's rationale, since guessing at unverified `SCStream` delegate wiring would be worse than an honest, clearly-marked stub. CDP (the spec's default) is fully implemented in Task 13.
- **Type consistency check:** `ScreenConfig`/`AudioConfig`/`AudioDevice` are defined once (Tasks 1, 2) and imported everywhere else they're used (Task 4's `app.py`, Task 14's `audio_capture.py`) rather than redefined. `driver.onMessage`/`getScreenContainer` (Task 9) are consumed with matching signatures in Tasks 10 and 11. `ScreenshotBroker.new_request()`'s returned `(request_id, future)` tuple shape matches its one call site in Task 6's own route.

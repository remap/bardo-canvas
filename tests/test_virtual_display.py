import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from ndi_broadcaster.virtual_display import (
    DisplayInfo,
    ensure_helper_built,
    start_vdisplay_helper,
)


def test_ensure_helper_built_skips_compilation_when_binary_is_up_to_date(tmp_path, monkeypatch):
    source = tmp_path / "main.swift"
    source.write_text("// source")
    header = tmp_path / "CGVirtualDisplayPrivate.h"
    header.write_text("// header")
    binary = tmp_path / "vdisplay_helper"
    binary.write_text("compiled")
    os.utime(source, (1000, 1000))
    os.utime(header, (1000, 1000))
    os.utime(binary, (2000, 2000))

    def fail_if_called(*args, **kwargs):
        pytest.fail("subprocess.run must not be called when the binary is up to date")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    result = ensure_helper_built(tmp_path)

    assert result == binary


def test_ensure_helper_built_compiles_when_binary_is_missing(tmp_path, monkeypatch):
    source = tmp_path / "main.swift"
    source.write_text("// source")
    header = tmp_path / "CGVirtualDisplayPrivate.h"
    header.write_text("// header")
    binary = tmp_path / "vdisplay_helper"
    calls = []

    def fake_run(args, check):
        calls.append(args)
        binary.write_text("compiled")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_helper_built(tmp_path)

    assert result == binary
    assert calls == [
        ["swiftc", "-O", "-o", str(binary), str(source), "-import-objc-header", str(header)]
    ]


def test_ensure_helper_built_recompiles_when_source_is_newer(tmp_path, monkeypatch):
    source = tmp_path / "main.swift"
    source.write_text("// source")
    header = tmp_path / "CGVirtualDisplayPrivate.h"
    header.write_text("// header")
    binary = tmp_path / "vdisplay_helper"
    binary.write_text("stale")
    os.utime(binary, (1000, 1000))
    os.utime(source, (2000, 2000))
    calls = []

    def fake_run(args, check):
        calls.append(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ensure_helper_built(tmp_path)

    assert len(calls) == 1


def test_ensure_helper_built_recompiles_when_only_header_is_newer(tmp_path, monkeypatch):
    # The header is also a compile input (via -import-objc-header) -- editing
    # only it, with main.swift untouched, must still trigger a rebuild rather
    # than silently keeping a binary compiled against the old header.
    source = tmp_path / "main.swift"
    source.write_text("// source")
    header = tmp_path / "CGVirtualDisplayPrivate.h"
    header.write_text("// header")
    binary = tmp_path / "vdisplay_helper"
    binary.write_text("stale")
    os.utime(source, (1000, 1000))
    os.utime(binary, (2000, 2000))
    os.utime(header, (3000, 3000))
    calls = []

    def fake_run(args, check):
        calls.append(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ensure_helper_built(tmp_path)

    assert len(calls) == 1


def test_start_vdisplay_helper_parses_json_report(monkeypatch):
    payload = json.dumps({"displayID": 69732865, "x": 5000, "y": 0, "width": 3840, "height": 2160})

    class _FakeProc:
        def __init__(self):
            self.stdout = _FakeStdout(payload + "\n")

    class _FakeStdout:
        def __init__(self, line):
            self._line = line

        def readline(self):
            return self._line

    fake_proc = _FakeProc()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: fake_proc)

    proc, info = start_vdisplay_helper(Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display")

    assert proc is fake_proc
    assert info == DisplayInfo(display_id=69732865, x=5000, y=0, width=3840, height=2160)


def test_start_vdisplay_helper_raises_when_no_output(monkeypatch):
    class _FakeStdout:
        def readline(self):
            return ""

    class _FakeStderr:
        def read(self):
            return "swiftc binary crashed"

    killed = threading.Event()

    class _FakeProc:
        stdout = _FakeStdout()
        stderr = _FakeStderr()

        def kill(self):
            killed.set()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _FakeProc())

    with pytest.raises(RuntimeError, match="swiftc binary crashed"):
        start_vdisplay_helper(Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display")

    assert killed.is_set(), "an empty-stdout failure must not leave the helper running"


def test_start_vdisplay_helper_kills_the_process_on_malformed_json(monkeypatch):
    # Valid line, but not JSON -- json.loads must not be allowed to leak the
    # helper it was given no chance to report a usable displayID for.
    class _FakeStdout:
        def readline(self):
            return "not json\n"

    killed = threading.Event()

    class _FakeProc:
        stdout = _FakeStdout()

        def kill(self):
            killed.set()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _FakeProc())

    with pytest.raises(json.JSONDecodeError):
        start_vdisplay_helper(Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display")

    assert killed.is_set(), "malformed JSON must not leave the helper running"


def test_start_vdisplay_helper_kills_the_process_when_json_is_missing_a_key(monkeypatch):
    # Valid JSON, but main.swift's contract (displayID/x/y/width/height) is
    # violated -- the KeyError below must still kill the process it was given
    # no way to hand back to the caller.
    payload = json.dumps({"x": 0, "y": 0, "width": 3840, "height": 2160})

    class _FakeStdout:
        def readline(self):
            return payload + "\n"

    killed = threading.Event()

    class _FakeProc:
        stdout = _FakeStdout()

        def kill(self):
            killed.set()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _FakeProc())

    with pytest.raises(KeyError):
        start_vdisplay_helper(Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display")

    assert killed.is_set(), "a missing JSON key must not leave the helper running"


def test_start_vdisplay_helper_raises_on_timeout_and_kills_the_process(monkeypatch):
    # A helper that never writes to stdout (WindowServer/permission stall)
    # must not block startup indefinitely -- confirm the bounded wait fires
    # and the stuck process gets killed rather than left running.
    class _FakeStdout:
        def readline(self):
            threading.Event().wait()  # blocks forever
            return ""

    killed = threading.Event()

    class _FakeProc:
        stdout = _FakeStdout()

        def kill(self):
            killed.set()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _FakeProc())

    with pytest.raises(TimeoutError, match="did not report"):
        start_vdisplay_helper(
            Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display", timeout_s=0.2
        )

    assert killed.is_set()

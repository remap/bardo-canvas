import json
import os
import subprocess
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

    class _FakeProc:
        stdout = _FakeStdout()
        stderr = _FakeStderr()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _FakeProc())

    with pytest.raises(RuntimeError, match="swiftc binary crashed"):
        start_vdisplay_helper(Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display")

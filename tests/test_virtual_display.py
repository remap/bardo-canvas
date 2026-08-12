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


class _RecordingProc:
    """A helper stub that records which signal path was taken.

    SIGKILL runs no handler in main.swift, so it never tears the virtual
    display down -- a helper killed outright leaves a live display in
    WindowServer with a dead owner, i.e. an unreclaimable zombie_b. Every
    failure path below must therefore reach `terminated`, and `killed` only
    ever as the escalation after SIGTERM was ignored.
    """

    def __init__(self, stdout, stderr=None, *, ignores_sigterm=False):
        self.stdout = stdout
        self.stderr = stderr
        self.ignores_sigterm = ignores_sigterm
        self.terminated = threading.Event()
        self.killed = threading.Event()

    def terminate(self):
        self.terminated.set()

    def kill(self):
        self.killed.set()

    def wait(self, timeout=None):
        if self.ignores_sigterm:
            raise subprocess.TimeoutExpired(cmd="vdisplay_helper", timeout=timeout)
        return 0


class _FakeStdout:
    def __init__(self, line):
        self._line = line

    def readline(self):
        return self._line


class _BlockingStdout:
    def readline(self):
        threading.Event().wait()  # blocks forever
        return ""


class _FakeStderr:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def test_start_vdisplay_helper_raises_when_no_output(monkeypatch):
    proc = _RecordingProc(_FakeStdout(""), _FakeStderr("swiftc binary crashed"))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: proc)

    with pytest.raises(RuntimeError, match="swiftc binary crashed"):
        start_vdisplay_helper(Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display")

    assert proc.terminated.is_set(), "an empty-stdout failure must not leave the helper running"
    assert not proc.killed.is_set(), "SIGKILL runs no handler, so it would leak the display"


def test_start_vdisplay_helper_terminates_the_process_on_malformed_json(monkeypatch):
    # Valid line, but not JSON -- json.loads must not be allowed to leak the
    # helper it was given no chance to report a usable displayID for.
    proc = _RecordingProc(_FakeStdout("not json\n"))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: proc)

    with pytest.raises(json.JSONDecodeError):
        start_vdisplay_helper(Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display")

    assert proc.terminated.is_set(), "malformed JSON must not leave the helper running"
    assert not proc.killed.is_set()


def test_start_vdisplay_helper_terminates_the_process_when_json_is_missing_a_key(monkeypatch):
    # Valid JSON, but main.swift's contract (displayID/x/y/width/height) is
    # violated. This path runs *after* the helper reported, so its display is
    # definitely live -- SIGTERM is the only signal that takes it back down.
    payload = json.dumps({"x": 0, "y": 0, "width": 3840, "height": 2160})
    proc = _RecordingProc(_FakeStdout(payload + "\n"))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: proc)

    with pytest.raises(KeyError):
        start_vdisplay_helper(Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display")

    assert proc.terminated.is_set(), "a missing JSON key must not leave the helper running"
    assert not proc.killed.is_set()


def test_start_vdisplay_helper_raises_on_timeout_and_terminates_the_process(monkeypatch):
    # A helper that never writes to stdout (WindowServer/permission stall)
    # must not block startup indefinitely. This is the likeliest path in
    # practice, and by 15s the helper has very probably already created and
    # applied its display -- so it must be SIGTERMed, not SIGKILLed.
    proc = _RecordingProc(_BlockingStdout())
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: proc)

    with pytest.raises(TimeoutError, match="did not report"):
        start_vdisplay_helper(
            Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display", timeout_s=0.2
        )

    assert proc.terminated.is_set()
    assert not proc.killed.is_set()


def test_start_vdisplay_helper_escalates_to_kill_when_sigterm_is_ignored(monkeypatch):
    # SIGKILL is still the right last resort: a helper that ignores SIGTERM
    # would otherwise be left running forever. The escalation must only ever
    # follow a SIGTERM that was given time to work.
    proc = _RecordingProc(_FakeStdout("not json\n"), ignores_sigterm=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: proc)

    with pytest.raises(json.JSONDecodeError):
        start_vdisplay_helper(Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display")

    assert proc.terminated.is_set()
    assert proc.killed.is_set(), "a helper that ignores SIGTERM must still be killed"

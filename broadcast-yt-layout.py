"""Run the layout-driver NDI broadcaster against yt-matrix's /layout page,
with its /layout-control operator console opened automatically.

This is project-specific plumbing, deliberately kept out of the shared
config/broadcaster.yaml -- that file stays app-agnostic (control_window_url:
null, the generic default every other layout-driver app gets), and this
script layers yt-matrix's own choices on top of it instead.

Two things need patching, both because layout-driver's own config assumes
its own server, not yt-matrix's:

1. The broadcaster's healthz check always derives from target_url by
   appending "/healthz" -- it assumes target_url IS the server root.
   yt-matrix's own /healthz lives at the root, not under /layout, so
   pointing target_url straight at /layout makes that derived check 404.
   This patches wait_for_healthy to check the real root instead, while
   target_url (what the captured browser actually navigates to) stays
   /layout.

2. control_window_url/control_display_name have no env-var override in
   layout-driver (unlike target_url's LAYOUT_DRIVER_TARGET_URL) -- they are
   read once, straight off the loaded BroadcasterConfig. This patches
   load_broadcaster_config to layer yt-matrix's control-window choice onto
   whatever config/broadcaster.yaml otherwise says, the same monkeypatch
   technique as (1) and for the same reason: the override belongs to this
   project, not to the shared config file.

Run from this repo (layout-driver), with its own uv env:
    uv run python broadcast-yt-layout.py

Also configures logging itself: launcher.py only calls logging.basicConfig
under its own `if __name__ == "__main__":` guard, which this script -- as a
separate entry point calling launcher.run() directly -- never executes.
Without this, every logger.info() call in launcher.py (including the
control-window placement diagnostics) is silently dropped by Python's
handler-of-last-resort, which is WARNING-and-up only.
"""

import logging
import os

os.environ["LAYOUT_DRIVER_TARGET_URL"] = "https://localhost:8444/layout"

from layout_server.log_format import log_format
from ndi_broadcaster import launcher

logging.basicConfig(level=logging.INFO, format=log_format(dict(os.environ)))

_real_wait_for_healthy = launcher.wait_for_healthy


def _wait_for_root_instead(_url, timeout_seconds):
    _real_wait_for_healthy("https://localhost:8444/healthz", timeout_seconds=timeout_seconds)


launcher.wait_for_healthy = _wait_for_root_instead

_real_load_broadcaster_config = launcher.load_broadcaster_config


def _load_with_yt_matrix_control_window(path):
    config = _real_load_broadcaster_config(path)
    return config.model_copy(
        update={
            "control_window_url": "https://localhost:8444/layout-control",
            "control_display_name": "Retina",
        }
    )


launcher.load_broadcaster_config = _load_with_yt_matrix_control_window

launcher.run()

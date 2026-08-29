"""Run the layout-driver NDI broadcaster against yt-matrix's /layout page.

The broadcaster's healthz check always derives from target_url by appending
"/healthz" -- it assumes target_url IS the server root. yt-matrix's own
/healthz lives at the root, not under /layout, so pointing target_url
straight at /layout makes that derived check 404. This patches
wait_for_healthy to check the real root instead, while target_url (what the
captured browser actually navigates to) stays /layout.

Run from this repo (layout-driver), with its own uv env:
    uv run python broadcast-yt-layout.py
"""

import os

os.environ["LAYOUT_DRIVER_TARGET_URL"] = "https://localhost:8444/layout"

from ndi_broadcaster import launcher

_real_wait_for_healthy = launcher.wait_for_healthy


def _wait_for_root_instead(_url, timeout_seconds):
    _real_wait_for_healthy("https://localhost:8444/healthz", timeout_seconds=timeout_seconds)


launcher.wait_for_healthy = _wait_for_root_instead

launcher.run()

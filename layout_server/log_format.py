from __future__ import annotations


def log_format(env: dict[str, str]) -> str:
    """A port prefix distinguishes one instance's log lines from another's when
    multiple instances run in the same terminal/log aggregator. Omitted when
    LAYOUT_DRIVER_PORT isn't set, so output outside run.sh is unchanged.

    Shared by layout_server.main and ndi_broadcaster.launcher -- one process
    per instance, both wanting the same prefix convention.
    """
    port = env.get("LAYOUT_DRIVER_PORT")
    prefix = f"[:{port}] " if port else ""
    return f"%(asctime)s {prefix}%(levelname)s %(name)s: %(message)s"

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from .app import create_app
from .certs import ensure_self_signed_cert
from .log_format import log_format

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
        app_static_dir=Path(
            env.get("APP_DIR", str(REPO_ROOT / "apps" / "test-pattern" / "static"))
        ),
        cert_path=Path(env.get("LAYOUT_DRIVER_SSL_CERT", str(runtime_dir / "cert.pem"))),
        key_path=Path(env.get("LAYOUT_DRIVER_SSL_KEY", str(runtime_dir / "key.pem"))),
        runtime_dir=runtime_dir,
        screens_yaml=Path(env.get("SCREENS_YAML", str(REPO_ROOT / "config" / "screens.yaml"))),
        audio_yaml=Path(env.get("AUDIO_YAML", str(REPO_ROOT / "config" / "audio.yaml"))),
    )


def main() -> None:
    env = dict(os.environ)
    logging.basicConfig(level=logging.INFO, format=log_format(env))
    settings = resolve_settings(env)
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

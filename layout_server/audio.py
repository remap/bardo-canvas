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
                AudioDevice(
                    index=index,
                    name=device["name"],
                    max_input_channels=device["max_input_channels"],
                )
            )
        if device["max_output_channels"] > 0:
            outputs.append(
                AudioDevice(
                    index=index,
                    name=device["name"],
                    max_output_channels=device["max_output_channels"],
                )
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

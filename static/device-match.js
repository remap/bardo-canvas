export function matchDeviceByName(name, devices) {
  if (!name) {
    return null;
  }
  const lowered = name.toLowerCase();
  return devices.find((device) => device.label.toLowerCase().includes(lowered)) ?? null;
}

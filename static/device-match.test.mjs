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

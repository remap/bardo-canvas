import { test } from "node:test";
import assert from "node:assert/strict";
import { nextReconnectDelay, RECONNECT_BASE_DELAY_MS, RECONNECT_MAX_DELAY_MS } from "./backoff.js";

test("nextReconnectDelay doubles the current delay", () => {
  assert.equal(nextReconnectDelay(1000), 2000);
  assert.equal(nextReconnectDelay(2000), 4000);
});

test("nextReconnectDelay caps at the max delay", () => {
  assert.equal(nextReconnectDelay(20000), RECONNECT_MAX_DELAY_MS);
  assert.equal(nextReconnectDelay(RECONNECT_MAX_DELAY_MS), RECONNECT_MAX_DELAY_MS);
});

test("a full backoff sequence from the base delay reaches and stays at the cap", () => {
  let delay = RECONNECT_BASE_DELAY_MS;
  const sequence = [delay];
  for (let i = 0; i < 6; i++) {
    delay = nextReconnectDelay(delay);
    sequence.push(delay);
  }
  assert.deepEqual(sequence, [1000, 2000, 4000, 8000, 16000, 30000, 30000]);
});

test("a custom max delay is respected", () => {
  assert.equal(nextReconnectDelay(4000, 5000), 5000);
});

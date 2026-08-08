import { test } from "node:test";
import assert from "node:assert/strict";
import { computeCoverFit, computeCompositePlacements } from "./geometry.js";

test("computeCoverFit crops a wider source for a 9:7 destination (screen F)", () => {
  const result = computeCoverFit(1920, 1080, 1800, 1400);
  assert.equal(result.sHeight, 1080);
  assert.ok(Math.abs(result.sWidth - 1080 * (1800 / 1400)) < 0.001);
  assert.ok(result.sx > 0);
  assert.equal(result.sy, 0);
  assert.deepEqual(
    { dx: result.dx, dy: result.dy, dWidth: result.dWidth, dHeight: result.dHeight },
    { dx: 0, dy: 0, dWidth: 1800, dHeight: 1400 }
  );
});

test("computeCoverFit crops a taller source for a 2:1 destination (screens B/C)", () => {
  const result = computeCoverFit(1000, 1000, 1200, 600);
  assert.equal(result.sWidth, 1000);
  assert.ok(Math.abs(result.sHeight - 500) < 0.001);
  assert.equal(result.sx, 0);
  assert.ok(result.sy > 0);
});

test("computeCoverFit crops a source for a 4:1 destination (screens D/A/E)", () => {
  const result = computeCoverFit(1600, 1600, 1600, 400);
  assert.equal(result.sWidth, 1600);
  assert.ok(Math.abs(result.sHeight - 400) < 0.001);
  assert.equal(result.sx, 0);
  assert.ok(result.sy > 0);
});

test("computeCoverFit returns full-frame params for an exact aspect match", () => {
  const result = computeCoverFit(1800, 1400, 1800, 1400);
  assert.equal(result.sx, 0);
  assert.equal(result.sy, 0);
  assert.equal(result.sWidth, 1800);
  assert.equal(result.sHeight, 1400);
});

test("computeCompositePlacements places each screen at its own rect", () => {
  const screens = [
    { id: "F", rect: { x: 220, y: 80, width: 1800, height: 1400 } },
    { id: "B", rect: { x: 2020, y: 80, width: 1200, height: 600 } },
  ];
  const placements = computeCompositePlacements(screens);
  assert.deepEqual(placements[0], { id: "F", dx: 220, dy: 80, dWidth: 1800, dHeight: 1400 });
  assert.deepEqual(placements[1], { id: "B", dx: 2020, dy: 80, dWidth: 1200, dHeight: 600 });
});

import { test } from "node:test";
import assert from "node:assert/strict";
import { validateSketchAssignments, sketchModulePath } from "./sketch-loader.js";

test("validateSketchAssignments accepts all six real screen/sketch pairs", () => {
  const config = {
    screens: [
      { id: "F", sketch: "flow-field" },
      { id: "B", sketch: "particle-swarm" },
      { id: "C", sketch: "laser-grid" },
      { id: "D", sketch: "mirror-ball" },
      { id: "A", sketch: "vu-bars" },
      { id: "E", sketch: "scanline-crt" },
    ],
  };
  const screens = validateSketchAssignments(config);
  assert.equal(screens.length, 6);
});

test("validateSketchAssignments throws loudly on an unrecognized sketch name", () => {
  const config = { screens: [{ id: "F", sketch: "not-a-real-sketch" }] };
  assert.throws(() => validateSketchAssignments(config), /Unknown sketch/);
});

test("sketchModulePath resolves a sketch name to its module path", () => {
  assert.equal(sketchModulePath("flow-field"), "./sketches/flow-field.js");
});

const KNOWN_SKETCHES = new Set([
  "flow-field",
  "particle-swarm",
  "laser-grid",
  "mirror-ball",
  "vu-bars",
  "scanline-crt",
]);

export function validateSketchAssignments(config) {
  for (const screen of config.screens) {
    if (!KNOWN_SKETCHES.has(screen.sketch)) {
      throw new Error(`Unknown sketch "${screen.sketch}" assigned to screen "${screen.id}"`);
    }
  }
  return config.screens;
}

export function sketchModulePath(sketchName) {
  return `./sketches/${sketchName}.js`;
}

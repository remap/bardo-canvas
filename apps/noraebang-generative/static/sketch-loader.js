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

export function validateScreenIds(screens, knownScreenIds) {
  const known = new Set(knownScreenIds);
  for (const screen of screens) {
    if (!known.has(screen.id)) {
      throw new Error(`Unknown screen id "${screen.id}" in noraebang.json (not in the layout)`);
    }
  }
  return screens;
}

export function sketchModulePath(sketchName) {
  return `./sketches/${sketchName}.js`;
}

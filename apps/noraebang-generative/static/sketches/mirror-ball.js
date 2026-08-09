import { BACKGROUND_COLOR, PALETTE } from "../theme.js";

const FACET_COLS = 24;
const FACET_ROWS = 18;

export function createSketch(width, height) {
  return function sketch(p) {
    let t = 0;
    const cellW = width / FACET_COLS;
    const cellH = height / FACET_ROWS;

    p.setup = () => {
      p.createCanvas(width, height);
      p.noStroke();
    };

    p.draw = () => {
      p.background(BACKGROUND_COLOR);
      t += 0.03;

      for (let row = 0; row < FACET_ROWS; row++) {
        for (let col = 0; col < FACET_COLS; col++) {
          const angle = p.noise(col * 0.3, row * 0.3, t) * p.TWO_PI;
          const glint = (Math.sin(angle * 4 + t * 2) + 1) / 2;
          if (glint > 0.82) {
            const color = PALETTE[(row + col) % PALETTE.length];
            p.fill(color);
            const size = p.map(glint, 0.82, 1, 2, Math.min(cellW, cellH) * 0.7);
            p.circle(col * cellW + cellW / 2, row * cellH + cellH / 2, size);
          }
        }
      }
    };
  };
}

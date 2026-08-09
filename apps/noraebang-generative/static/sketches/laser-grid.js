import { BACKGROUND_COLOR, PALETTE } from "../theme.js";

const GRID_SPACING = 90;

export function createSketch(width, height) {
  return function sketch(p) {
    let t = 0;

    p.setup = () => {
      p.createCanvas(width, height);
    };

    p.draw = () => {
      p.background(BACKGROUND_COLOR);
      t += 0.02;

      p.strokeWeight(2);
      for (let x = 0; x <= width; x += GRID_SPACING) {
        const wobble = Math.sin(t + x * 0.01) * 30;
        const color = PALETTE[Math.floor(x / GRID_SPACING) % PALETTE.length];
        p.stroke(color);
        p.line(x + wobble, 0, x - wobble, height);
      }

      const sweepY = ((Math.sin(t * 0.6) + 1) / 2) * height;
      p.strokeWeight(4);
      p.stroke(PALETTE[0]);
      p.line(0, sweepY, width, sweepY);
    };
  };
}

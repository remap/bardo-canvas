import { PALETTE } from "../theme.js";

const SCANLINE_SPACING = 4;

export function createSketch(width, height) {
  return function sketch(p) {
    let t = 0;

    p.setup = () => {
      p.createCanvas(width, height);
    };

    p.draw = () => {
      p.background(4, 2, 7);
      t += 1;

      p.noStroke();
      p.fill(255, 255, 255, 6);
      for (let y = 0; y < height; y += SCANLINE_SPACING) {
        p.rect(0, y, width, 1);
      }

      p.stroke(PALETTE[Math.floor(t / 40) % PALETTE.length]);
      p.strokeWeight(3);
      const glitchY = p.noise(t * 0.05) * height;
      const glitchOffset = (p.noise(t * 0.1) - 0.5) * 40;
      p.line(0, glitchY, width, glitchY + glitchOffset);

      if (p.random() < 0.03) {
        p.noStroke();
        p.fill(255, 255, 255, 20);
        p.rect(0, p.random(height), width, p.random(2, 12));
      }
    };
  };
}

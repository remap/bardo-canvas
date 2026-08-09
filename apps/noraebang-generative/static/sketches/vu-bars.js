import { PALETTE } from "../theme.js";

const BAR_COUNT = 32;

export function createSketch(width, height) {
  return function sketch(p) {
    let t = 0;
    const barWidth = width / BAR_COUNT;

    p.setup = () => {
      p.createCanvas(width, height);
      p.noStroke();
    };

    p.draw = () => {
      p.background(4, 2, 7);
      t += 0.05;

      for (let i = 0; i < BAR_COUNT; i++) {
        const level =
          ((Math.sin(t + i * 0.4) + 1) / 2) * 0.6 + ((Math.sin(t * 2.3 + i * 0.15) + 1) / 2) * 0.4;
        const barHeight = level * height;
        p.fill(PALETTE[i % PALETTE.length]);
        p.rect(i * barWidth + 2, height - barHeight, barWidth - 4, barHeight);
      }
    };
  };
}

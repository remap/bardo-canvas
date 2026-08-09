import { BACKGROUND_COLOR, PALETTE } from "../theme.js";

const PARTICLE_COUNT = 350;
const NOISE_SCALE = 0.0025;
const STEP_SIZE = 2.2;

export function createSketch(width, height) {
  return function sketch(p) {
    let particles = [];
    let zOffset = 0;

    p.setup = () => {
      p.createCanvas(width, height);
      particles = Array.from({ length: PARTICLE_COUNT }, () => ({
        x: p.random(width),
        y: p.random(height),
        color: p.random(PALETTE),
      }));
      p.background(BACKGROUND_COLOR);
    };

    p.draw = () => {
      p.noStroke();
      p.fill(5, 2, 8, 18);
      p.rect(0, 0, width, height);

      zOffset += 0.002;
      for (const particle of particles) {
        const angle =
          p.noise(particle.x * NOISE_SCALE, particle.y * NOISE_SCALE, zOffset) * p.TWO_PI * 3;
        particle.x += Math.cos(angle) * STEP_SIZE;
        particle.y += Math.sin(angle) * STEP_SIZE;
        if (particle.x < 0) particle.x += width;
        if (particle.x > width) particle.x -= width;
        if (particle.y < 0) particle.y += height;
        if (particle.y > height) particle.y -= height;

        p.fill(particle.color);
        p.circle(particle.x, particle.y, 2.5);
      }
    };
  };
}

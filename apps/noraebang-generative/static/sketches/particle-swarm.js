import { BACKGROUND_COLOR, PALETTE } from "../theme.js";

const BOID_COUNT = 90;
const MAX_SPEED = 2.4;
const NEIGHBOR_RADIUS = 60;

export function createSketch(width, height) {
  return function sketch(p) {
    let boids = [];

    p.setup = () => {
      p.createCanvas(width, height);
      boids = Array.from({ length: BOID_COUNT }, () => ({
        pos: p.createVector(p.random(width), p.random(height)),
        vel: p5.Vector.random2D().mult(MAX_SPEED),
        color: p.random(PALETTE),
      }));
      p.background(BACKGROUND_COLOR);
    };

    p.draw = () => {
      p.noStroke();
      p.fill(BACKGROUND_COLOR + "1E");
      p.rect(0, 0, width, height);

      for (const boid of boids) {
        const alignment = p.createVector(0, 0);
        const cohesion = p.createVector(0, 0);
        const separation = p.createVector(0, 0);
        let neighborCount = 0;

        for (const other of boids) {
          if (other === boid) continue;
          const d = p.dist(boid.pos.x, boid.pos.y, other.pos.x, other.pos.y);
          if (d < NEIGHBOR_RADIUS) {
            alignment.add(other.vel);
            cohesion.add(other.pos);
            const away = p5.Vector.sub(boid.pos, other.pos).div(Math.max(d, 1));
            separation.add(away);
            neighborCount++;
          }
        }

        if (neighborCount > 0) {
          alignment.div(neighborCount).setMag(0.05);
          cohesion.div(neighborCount).sub(boid.pos).setMag(0.03);
          separation.setMag(0.08);
          boid.vel.add(alignment).add(cohesion).add(separation);
          boid.vel.limit(MAX_SPEED);
        }

        boid.pos.add(boid.vel);
        if (boid.pos.x < 0) boid.pos.x += width;
        if (boid.pos.x > width) boid.pos.x -= width;
        if (boid.pos.y < 0) boid.pos.y += height;
        if (boid.pos.y > height) boid.pos.y -= height;

        p.fill(boid.color);
        p.circle(boid.pos.x, boid.pos.y, 5);
      }
    };
  };
}

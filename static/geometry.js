export function computeCoverFit(sourceWidth, sourceHeight, destWidth, destHeight) {
  const sourceAspect = sourceWidth / sourceHeight;
  const destAspect = destWidth / destHeight;

  let sx, sy, sWidth, sHeight;
  if (sourceAspect > destAspect) {
    sHeight = sourceHeight;
    sWidth = sourceHeight * destAspect;
    sx = (sourceWidth - sWidth) / 2;
    sy = 0;
  } else {
    sWidth = sourceWidth;
    sHeight = sourceWidth / destAspect;
    sx = 0;
    sy = (sourceHeight - sHeight) / 2;
  }

  return { sx, sy, sWidth, sHeight, dx: 0, dy: 0, dWidth: destWidth, dHeight: destHeight };
}

export function computeCompositePlacements(screens) {
  return screens.map((screen) => ({
    id: screen.id,
    dx: screen.rect.x,
    dy: screen.rect.y,
    dWidth: screen.rect.width,
    dHeight: screen.rect.height,
  }));
}

// Composites the wall and PNG-encodes it entirely off the main thread.
//
// Split out of layout-driver.js because a screenshot_request landing on the
// main thread while the NDI broadcaster's sck (ScreenCaptureKit) capture
// backend has an SCStream attached to this page's window was found, live, to
// make ScreenCaptureKit permanently stop delivering newly captured frames for
// the rest of that broadcaster process's life -- reproduced repeatedly by
// pushing a screen image immediately followed by a screenshot request, and
// never by either alone. Compositing and encoding here, from cheap
// createImageBitmap() snapshots handed over by the main thread rather than a
// live drawImage() read there, keeps this path from ever touching the main
// thread's own render/compositor scheduling, regardless of the exact
// mechanism behind that stall.
self.onmessage = async (event) => {
  const { requestId, width, height, placements, bitmaps } = event.data;

  const canvas = new OffscreenCanvas(width, height);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "black";
  ctx.fillRect(0, 0, width, height);

  placements.forEach((placement, i) => {
    const bitmap = bitmaps[i];
    if (!bitmap) {
      return;
    }
    ctx.drawImage(bitmap, placement.dx, placement.dy, placement.dWidth, placement.dHeight);
    bitmap.close();
  });

  const blob = await canvas.convertToBlob({ type: "image/png" });
  self.postMessage({ requestId, blob });
};

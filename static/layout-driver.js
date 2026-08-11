import { computeCoverFit } from "./geometry.js";
import { matchDeviceByName } from "./device-match.js";
import { nextReconnectDelay, RECONNECT_BASE_DELAY_MS } from "./backoff.js";

async function fetchScreens() {
  const response = await fetch("/api/screens");
  if (!response.ok) {
    throw new Error(`Failed to fetch /api/screens: ${response.status}`);
  }
  return response.json();
}

function buildRoot(layoutConfig) {
  // config/screens.yaml is the single source of truth for canvas dimensions.
  const canvasWidth = layoutConfig.canvas.width;
  const canvasHeight = layoutConfig.canvas.height;

  document.body.style.margin = "0";
  document.body.style.overflow = "hidden";
  document.body.style.background = "black";

  const root = document.createElement("div");
  root.id = "layout-driver-root";
  root.style.position = "absolute";
  root.style.top = "0";
  root.style.left = "0";
  root.style.width = `${canvasWidth}px`;
  root.style.height = `${canvasHeight}px`;
  root.style.background = "black";
  root.style.transformOrigin = "top left";
  document.body.appendChild(root);

  const containers = new Map();
  for (const screen of layoutConfig.screens) {
    const container = document.createElement("div");
    container.id = `screen-${screen.id}`;
    container.style.position = "absolute";
    container.style.left = `${screen.rect.x}px`;
    container.style.top = `${screen.rect.y}px`;
    container.style.width = `${screen.rect.width}px`;
    container.style.height = `${screen.rect.height}px`;
    container.style.overflow = "hidden";
    root.appendChild(container);
    containers.set(screen.id, {
      element: container,
      width: screen.rect.width,
      height: screen.rect.height,
      x: screen.rect.x,
      y: screen.rect.y,
    });
  }

  function rescale() {
    const scale = Math.min(window.innerWidth / canvasWidth, window.innerHeight / canvasHeight);
    root.style.transform = `scale(${scale})`;
  }
  window.addEventListener("resize", rescale);
  rescale();

  return { containers, root };
}

function connectWebSocket(handlers, onConnectionChange) {
  let reconnectDelayMs = RECONNECT_BASE_DELAY_MS;

  function connect() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
    ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      for (const handler of handlers) {
        handler(message);
      }
    });
    ws.addEventListener("open", () => {
      reconnectDelayMs = RECONNECT_BASE_DELAY_MS;
      onConnectionChange(true);
      for (const handler of handlers) {
        handler({ type: "_connected" });
      }
    });
    ws.addEventListener("close", () => {
      onConnectionChange(false);
      setTimeout(connect, reconnectDelayMs);
      reconnectDelayMs = nextReconnectDelay(reconnectDelayMs);
    });
    return ws;
  }
  return connect();
}

export async function initLayoutDriver() {
  const layoutConfig = await fetchScreens();
  const { containers, root } = buildRoot(layoutConfig);
  const messageHandlers = [];
  let isConnected = false;

  const driver = {
    layoutConfig,
    getScreenContainer(id) {
      const container = containers.get(id);
      if (!container) {
        throw new Error(`Unknown screen id: ${id}`);
      }
      return container;
    },
    // Measures each screen's ACTUAL rendered position/size, rather than
    // recomputing it from layoutConfig.screens[].rect -- config and the real
    // DOM are supposed to agree, but a CSS bug once silently broke that
    // agreement for every screen but the first (position:relative overriding
    // buildRoot()'s position:absolute), invisibly, because compositing used
    // to trust the config value instead of checking. Anything that
    // composites the wall (the cdp capture path, /api/screenshot) calls this
    // so it can never again describe a different layout than what's actually
    // on screen -- if the real layout is ever broken again, every consumer
    // breaks the same way, visibly, instead of only the one nobody is
    // capturing from at the time.
    measureScreenPlacements() {
      const rootRect = root.getBoundingClientRect();
      const scale = rootRect.width / layoutConfig.canvas.width;
      return layoutConfig.screens.map((screen) => {
        const rect = this.getScreenContainer(screen.id).element.getBoundingClientRect();
        return {
          id: screen.id,
          dx: (rect.left - rootRect.left) / scale,
          dy: (rect.top - rootRect.top) / scale,
          dWidth: rect.width / scale,
          dHeight: rect.height / scale,
        };
      });
    },
    onMessage(handler) {
      messageHandlers.push(handler);
      // App code may do async work between initLayoutDriver() and registering its
      // handler, by which point the socket's "open" event has already fired. Replay
      // it so late registrants still get their initial resync.
      if (isConnected) {
        handler({ type: "_connected" });
      }
    },
  };

  connectWebSocket(messageHandlers, (connected) => {
    isConnected = connected;
  });

  return driver;
}

// Generic dev-convenience feature, usable by any app: the server watches its
// static directory (and the shared framework one) for changes and broadcasts
// a "reload" message over the same WebSocket every client already holds --
// see layout_server/file_watcher.py. Opt-in (not automatic in
// initLayoutDriver()) so a production broadcast never reloads unexpectedly
// just because this function wasn't intentionally called.
export function enableAutoReload(driver) {
  driver.onMessage((message) => {
    if (message.type === "reload") {
      window.location.reload();
    }
  });
}

// The simplest possible app mode: one static HTML file per screen, loaded
// once into an iframe sized to fill that screen's area. No JavaScript, no
// image-push API, no build step -- editing {screenId}.html and reloading
// the page is the entire workflow, meant for people with no coding
// background. See apps/static-pages for a working example of every screen.
//
// Only works correctly with the sck capture backend (captures real screen
// pixels directly). compositeToCanvas() (the cdp backend's
// __ndiCaptureDataURL(), and enableScreenshotResponder()'s /api/screenshot)
// both composite by drawing each screen's <canvas> element with
// ctx.drawImage() -- an iframe has no canvas to draw, so both would produce
// a blank/black result for any screen using this mode. Don't call
// enableScreenshotResponder() in an app that uses this.
export function enableStaticPageMode(driver) {
  for (const screen of driver.layoutConfig.screens) {
    const container = driver.getScreenContainer(screen.id);
    const iframe = document.createElement("iframe");
    iframe.src = `${screen.id}.html`;
    iframe.style.display = "block";
    iframe.style.width = "100%";
    iframe.style.height = "100%";
    iframe.style.border = "none";
    container.element.appendChild(iframe);
  }
}

function drawCoverFit(ctx, image, canvasWidth, canvasHeight) {
  const fit = computeCoverFit(image.width, image.height, canvasWidth, canvasHeight);
  ctx.drawImage(image, fit.sx, fit.sy, fit.sWidth, fit.sHeight, fit.dx, fit.dy, fit.dWidth, fit.dHeight);
}

async function loadScreenImage(screenId, version) {
  const response = await fetch(`/screens/${screenId}/image?v=${version}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch image for ${screenId}: ${response.status}`);
  }
  const blob = await response.blob();
  return createImageBitmap(blob);
}

export function enableImageMode(driver) {
  const layers = new Map();

  for (const screen of driver.layoutConfig.screens) {
    const container = driver.getScreenContainer(screen.id);
    // Do NOT set position here: buildRoot() already made this container
    // position:absolute (placed at screen.rect.x/y within #layout-driver-root).
    // That's already a valid containing block for this function's
    // position:absolute canvases -- overwriting it to "relative" (as this line
    // used to) breaks the container's own absolute placement: a
    // position:relative element's top/left become an OFFSET from its normal
    // document-flow position instead of an absolute coordinate, so every
    // screen after the first got shifted down by the combined height of every
    // container before it in DOM order. Confirmed live: this only ever looked
    // correct via /api/screenshot, which composites from screen.rect directly
    // and never touches actual DOM layout -- the real rendered page (what SCK
    // and any real screenshot of the browser window actually capture) was
    // broken for every screen except the first the whole time.

    const canvasA = document.createElement("canvas");
    const canvasB = document.createElement("canvas");
    for (const canvas of [canvasA, canvasB]) {
      canvas.width = container.width;
      canvas.height = container.height;
      canvas.style.position = "absolute";
      canvas.style.top = "0";
      canvas.style.left = "0";
      canvas.style.transition = "opacity 300ms linear";
      container.element.appendChild(canvas);
    }
    canvasB.style.opacity = "0";
    canvasA.dataset.layoutDriverActive = "true";

    layers.set(screen.id, { canvases: [canvasA, canvasB], activeIndex: 0, pending: Promise.resolve() });
  }

  async function runApplyFrame(layer, screenId, version, transitionMs) {
    const image = await loadScreenImage(screenId, version);
    const nextIndex = 1 - layer.activeIndex;
    const nextCanvas = layer.canvases[nextIndex];
    const currentCanvas = layer.canvases[layer.activeIndex];

    drawCoverFit(nextCanvas.getContext("2d"), image, nextCanvas.width, nextCanvas.height);
    nextCanvas.style.transition = `opacity ${transitionMs}ms linear`;
    currentCanvas.style.transition = `opacity ${transitionMs}ms linear`;
    nextCanvas.style.opacity = "1";
    currentCanvas.style.opacity = "0";
    layer.activeIndex = nextIndex;
    nextCanvas.dataset.layoutDriverActive = "true";
    delete currentCanvas.dataset.layoutDriverActive;
  }

  async function applyFrame(screenId, version, transitionMs) {
    const layer = layers.get(screenId);
    if (!layer) {
      return;
    }
    const current = layer.pending
      .catch(() => {})
      .then(() => runApplyFrame(layer, screenId, version, transitionMs));
    layer.pending = current;
    return current;
  }

  async function resync() {
    for (const screenId of layers.keys()) {
      try {
        await applyFrame(screenId, Date.now(), 0);
      } catch {
        // No image has been pushed for this screen yet — leave it blank.
      }
    }
  }

  driver.onMessage((message) => {
    if (message.type === "frame") {
      applyFrame(message.screen, message.version, message.transition_ms);
    } else if (message.type === "_connected") {
      resync();
    }
  });
}

export function enableScreenshotResponder(driver) {
  function findCanvas(screenId) {
    const container = driver.getScreenContainer(screenId);
    return (
      container.element.querySelector("canvas[data-layout-driver-active='true']") ??
      container.element.querySelector("canvas")
    );
  }

  // Created once and reused, not allocated fresh per call: __ndiCaptureDataURL()
  // (below) is called up to config.fps times per second, sustained for the life of
  // the broadcast. A fresh canvas().createElement() per call -- fine at this
  // function's original call rate (at most once per screenshot_request, i.e. rare)
  // -- leaked a GPU-backed canvas buffer every call at that rate, which live testing
  // traced to two failure modes depending on GPU backend: a slow climb in capture
  // latency (Metal, GPU resources exhausted gradually) and an outright renderer
  // crash after ~30s (SwiftShader, software-side allocation exhausted faster).
  const offscreen = document.createElement("canvas");
  offscreen.width = driver.layoutConfig.canvas.width;
  offscreen.height = driver.layoutConfig.canvas.height;
  const offscreenCtx = offscreen.getContext("2d");

  function compositeToCanvas() {
    offscreenCtx.fillStyle = "black";
    offscreenCtx.fillRect(0, 0, offscreen.width, offscreen.height);

    for (const placement of driver.measureScreenPlacements()) {
      const canvas = findCanvas(placement.id);
      if (!canvas) {
        continue;
      }
      offscreenCtx.drawImage(canvas, placement.dx, placement.dy, placement.dWidth, placement.dHeight);
    }
    return offscreen;
  }

  // The screenshot responder composites and PNG-encodes in screenshot-worker.js,
  // not via compositeToCanvas() above -- see that file for why (a real,
  // live-observed ScreenCaptureKit capture stall). compositeToCanvas() itself
  // is still used, unchanged, by __ndiCaptureDataURL()'s cdp-path capture below.
  const screenshotWorker = new Worker(new URL("./screenshot-worker.js", import.meta.url));
  let nextScreenshotRequestId = 0;
  const pendingScreenshotRequests = new Map();
  // Requests are cleaned up by whichever fires first -- an onmessage reply
  // or the per-request timeout below -- so a worker-side error/crash (which
  // never posts a reply) can't leak a pending promise for the life of the page.
  const SCREENSHOT_WORKER_TIMEOUT_MS = 10000;
  screenshotWorker.onmessage = (event) => {
    const { requestId, blob } = event.data;
    const resolve = pendingScreenshotRequests.get(requestId);
    if (resolve) {
      pendingScreenshotRequests.delete(requestId);
      resolve(blob);
    }
  };
  screenshotWorker.onerror = (event) => {
    console.error("screenshot worker error:", event.message);
  };

  async function composite() {
    const placements = driver.measureScreenPlacements();
    // createImageBitmap() snapshots each canvas's current pixel buffer without
    // synchronously reading it on the main thread -- the actual compositing draw
    // calls and PNG encode happen in the worker, off pixel data transferred here
    // at zero copy cost.
    const bitmaps = await Promise.all(
      placements.map((placement) => {
        const canvas = findCanvas(placement.id);
        return canvas ? createImageBitmap(canvas) : null;
      })
    );

    const requestId = nextScreenshotRequestId++;
    const result = new Promise((resolve) => {
      const timeoutId = setTimeout(() => {
        if (pendingScreenshotRequests.delete(requestId)) {
          console.error(`screenshot worker request ${requestId} timed out; resolving with no data`);
          resolve(null);
        }
      }, SCREENSHOT_WORKER_TIMEOUT_MS);
      pendingScreenshotRequests.set(requestId, (blob) => {
        clearTimeout(timeoutId);
        resolve(blob);
      });
    });
    screenshotWorker.postMessage(
      {
        requestId,
        width: driver.layoutConfig.canvas.width,
        height: driver.layoutConfig.canvas.height,
        placements,
        bitmaps,
      },
      bitmaps.filter((bitmap) => bitmap !== null)
    );
    return result;
  }

  // Exposed for the NDI broadcaster to read directly via page.evaluate() -- CDP's own
  // screenshot/screencast APIs (Page.captureScreenshot, Page.startScreencast) were
  // found, live, to return solid black for this app's canvases despite them having
  // provably-correct pixel data (confirmed via ctx.getImageData() at the exact same
  // coordinates, reproduced with/without GPU acceleration and across both Chromium
  // binaries Playwright can select -- a real bug in Chromium's viewport-level capture
  // for this canvas-drawing pattern, not a timing or config issue on our end).
  // toDataURL() reads the canvas's own buffer directly and was proven reliable
  // throughout that investigation; page.evaluate() runs over the same CDP connection
  // Playwright already holds to drive this browser -- no HTTP/network round trip.
  window.__ndiCaptureDataURL = () => compositeToCanvas().toDataURL("image/jpeg", 0.85);

  driver.onMessage(async (message) => {
    if (message.type !== "screenshot_request") {
      return;
    }
    const blob = await composite();
    if (blob === null) {
      // Worker crashed or timed out (see SCREENSHOT_WORKER_TIMEOUT_MS above)
      // -- nothing to post. The server's own shorter timeout on this
      // request has normally already given up by now regardless.
      return;
    }
    await fetch(`/api/screenshot-result/${message.request_id}`, { method: "POST", body: blob });
  });

  return { composite };
}

async function requestMicrophoneAccessWithTimeout(timeoutMs) {
  return Promise.race([
    navigator.mediaDevices.getUserMedia({ audio: true }).catch(() => null),
    new Promise((resolve) => setTimeout(() => resolve(null), timeoutMs)),
  ]);
}

export async function routeAudioElement(el) {
  const response = await fetch("/api/audio-config");
  const config = await response.json();
  if (!config.enabled || !config.output_device) {
    return;
  }

  await requestMicrophoneAccessWithTimeout(3000);
  const devices = await navigator.mediaDevices.enumerateDevices();
  const outputs = devices
    .filter((device) => device.kind === "audiooutput")
    .map((device) => ({ deviceId: device.deviceId, label: device.label }));

  const match = matchDeviceByName(config.output_device, outputs);
  if (!match) {
    console.warn(`Audio output device not found: ${config.output_device}`);
    return;
  }
  if (typeof el.setSinkId === "function") {
    await el.setSinkId(match.deviceId);
  }
}

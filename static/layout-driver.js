import { computeCoverFit } from "./geometry.js";

const CANVAS_WIDTH = 3840;
const CANVAS_HEIGHT = 2160;

async function fetchScreens() {
  const response = await fetch("/api/screens");
  if (!response.ok) {
    throw new Error(`Failed to fetch /api/screens: ${response.status}`);
  }
  return response.json();
}

function buildRoot(layoutConfig) {
  document.body.style.margin = "0";
  document.body.style.overflow = "hidden";
  document.body.style.background = "black";

  const root = document.createElement("div");
  root.id = "layout-driver-root";
  root.style.position = "absolute";
  root.style.top = "0";
  root.style.left = "0";
  root.style.width = `${CANVAS_WIDTH}px`;
  root.style.height = `${CANVAS_HEIGHT}px`;
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
    const scale = Math.min(window.innerWidth / CANVAS_WIDTH, window.innerHeight / CANVAS_HEIGHT);
    root.style.transform = `scale(${scale})`;
  }
  window.addEventListener("resize", rescale);
  rescale();

  return containers;
}

function connectWebSocket(handlers) {
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
      for (const handler of handlers) {
        handler({ type: "_connected" });
      }
    });
    ws.addEventListener("close", () => {
      setTimeout(connect, 1000);
    });
    return ws;
  }
  return connect();
}

export async function initLayoutDriver() {
  const layoutConfig = await fetchScreens();
  const containers = buildRoot(layoutConfig);
  const messageHandlers = [];

  const driver = {
    layoutConfig,
    getScreenContainer(id) {
      const container = containers.get(id);
      if (!container) {
        throw new Error(`Unknown screen id: ${id}`);
      }
      return container;
    },
    onMessage(handler) {
      messageHandlers.push(handler);
    },
  };

  connectWebSocket(messageHandlers);

  return driver;
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
    container.element.style.position = "relative";

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

  return { canvases: new Map([...layers].map(([id, layer]) => [id, layer.canvases[layer.activeIndex]])) };
}

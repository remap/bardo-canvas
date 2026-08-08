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

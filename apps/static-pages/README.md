# static-pages

The simplest way to put something on the LED wall: one plain HTML file per screen,
no coding experience required.

## How it works

Each screen on the wall has a matching HTML file in `static/`:

- `F.html` → Screen F
- `B.html` → Screen B
- `C.html` → Screen C
- `D.html` → Screen D
- `A.html` → Screen A
- `E.html` → Screen E

To change what appears on a screen, open its file in any text editor, change it, and
save. Reload the page (or restart the broadcaster) to see your change. You can edit
the text and colors already there, or delete everything and paste in your own HTML
from scratch — each file is a complete, independent web page. There is no build step
and nothing else to configure.

(Which letter goes where on the physical wall is set once, by whoever configures
`config/screens.yaml` — you don't need to touch that file to use this app.)

## Running it

From the repo root:

```bash
./run.sh apps/static-pages/static
```

Then open `https://localhost:8443/` in a browser to see all six screens composited
together (click through the certificate warning — that's expected, see the main
README). That preview is the exact same page the NDI broadcaster captures.

**Requires the `sck` capture backend** (`config/broadcaster.yaml`'s
`capture_backend: "sck"`, already the default — see the main README's "sck capture
backend" section). Each screen here is a separate embedded page (an iframe), and only
`sck` captures real screen pixels directly; the older `cdp` backend and
`/api/screenshot` both work by drawing each screen's own canvas, and an iframe has no
canvas for them to draw, so switching to `capture_backend: "cdp"` would broadcast a
black screen for every screen this app uses.

# Vendored dependencies

## p5.js

- Version: 1.11.3
- Source: https://cdn.jsdelivr.net/npm/p5@1.11.3/lib/p5.min.js
- License: LGPL-2.1 (see https://github.com/processing/p5.js/blob/main/license.txt)
- sha256: af51e6211e061b5ae463fbc5c3c1c272e5ca67fa560ed3513fde17325d837506

Loaded as a plain global script (`<script src="/vendor/p5.min.js">`), not an ES
module — it attaches `p5` to `window`, which app modules reference directly.
Vendored locally rather than CDN-loaded so this app doesn't depend on live
internet access at showtime.

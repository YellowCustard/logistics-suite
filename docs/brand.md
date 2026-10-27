# Brand

Nuronteq Brand Identity v1.0 (PDF, held by Adeel; raster only). What the code uses lives in
`apps/platform/static/brand/`:

| File | Use |
|---|---|
| `tokens.css` | Colour and type tokens (`--nq-*`) and `@font-face` for the self-hosted fonts |
| `fonts/` | Latin subsets of Inter (variable), Space Grotesk 600, JetBrains Mono 500 from Fontsource 5.3.0, SIL OFL 1.1 |
| `mark-light.svg`, `mark-dark.svg` | Full-colour mark for light and dark grounds |
| `mark-reverse.svg`, `mark-mono.svg` | One-colour white (on Indigo or photos) and Ink (print) |
| `mark-simple.svg` | Below 40 px: route, outer nodes and centre node only |
| `favicon.svg`, `app-icon.svg`, `icon-*.png`, `apple-touch-icon.png` | Browser and PWA icons on Ink |
| `admin.css` | Django admin skin (variable overrides only), wired in by `apps/platform/templates/admin/base_site.html` |

The SVG marks were redrawn from the PDF's primary lockup on a 512 grid; swap them for the designer's vector
files when those arrive (keep the file names). PNG icons are renders of the SVGs at 192, 512 and 180 px.

Rules from the identity that code should keep: the wordmark is always lowercase `nuronteq` in Space Grotesk 600;
Signal Cyan is for highlights and live status, never body text on white; body text is Inter at 16 px or more;
uppercase labels and metrics use JetBrains Mono with +10% tracking. Anything the scanner renders must be
self-hosted and listed in `apps/scanner/views.py` `ASSETS`, because its CSP is `'self'` only and it runs offline.

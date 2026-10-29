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
| `admin.css` | Django admin skin: the `--nq-*` theme tokens and each Django admin variable mapped to one of them; wired in by `apps/platform/templates/admin/base_site.html` |

The marks come from the designer's vector neural-N (`nuronteq-neural-n`, 64-unit grid): `mark-light.svg` reproduces
its geometry and colours exactly, the other variants change only colours or drop elements as the identity specifies (Indigo
becomes `#6E6EFF` on Ink). PNG icons are renders of the SVGs at 192, 512 and 180 px.

Rules from the identity that code should keep: the wordmark is always lowercase `nuronteq` in Space Grotesk 600;
Signal Cyan is for highlights and live status, never body text on white; body text is Inter at 16 px or more;
uppercase labels and metrics use JetBrains Mono with +10% tracking. Anything the scanner renders must be
self-hosted and listed in `apps/scanner/views.py` `ASSETS`, because its CSP is `'self'` only and it runs offline.

## Admin shell

The Django admin wears a Tailwind shell (sidebar, Ctrl/Cmd+K palette, KPI home, phone bottom bar) built from template
overrides in `apps/platform/templates/admin/` (`base_site.html`, `index.html`, `nq/*`), `nq-admin.js` (no dependencies)
and one compiled stylesheet. There is no Node at runtime.

- Source: `portal/admin-ui/nq-admin.css`. Output: `apps/platform/static/nq-admin/nq-admin.css` (committed).
  After changing classes in the admin templates, run `npm run admin-css` in `portal/` and commit the output; CI fails on drift.
- `nq-admin/django-layer.css` replaces the stock `base.css` link and imports it into the lowest cascade layer, so utilities
  win over its element rules. Never use the bare `hidden` class in admin templates (Django's `.hidden` is `!important`);
  use `max-lg:hidden`.
- KPI cards and status chips come from `apps/platform/admin_dashboard.py`. Business modules register providers in their
  `AppConfig.ready()` (see `apps/*/admin_dashboard.py`); each is permission-gated, runs in a savepoint and is skipped
  (and logged) if it fails.

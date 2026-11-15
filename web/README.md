# Whoopy Studio

The default local UI is the SvelteKit Studio. `whoopy web --open` serves the bundled UI from `src/whoopy/webui/static`; no Node.js process or external service is needed to use it.

The Studio includes Home, Create, Voices, Library and Settings. Walking lines is the default background; Settings > Backgrounds holds the other designs and light/dark appearances. Companion, cover, folder and course-outline choices are stored on this device. Course outlines do not generate course audio yet.

## Frontend development

```bash
cd web
npm ci
npm run dev:studio
```

Run the Python API separately with `uv run --offline whoopy web`. The development server on port 5173 proxies `/api` to the local Python server on port 8765. The Studio supports the versioned API and the default server provides compatibility with the established generation service. It offers only writing profiles and installed voices that the server supports.

```bash
npm run check
npm test
npm run build
```

Commit both source and the complete generated `src/whoopy/webui/static` bundle. Builds have a source fingerprint so CI can verify that packaged output matches source. Screenshots, design comparisons, sample-data servers and other local review artifacts are excluded from the product.

The separate generation/developer tooling remains outside this UI release.

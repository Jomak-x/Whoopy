import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// A source fingerprint keeps rebuilds reproducible while detecting UI upgrades.
const webRoot = new URL('../../', import.meta.url);
const fingerprint = createHash('sha256');
function include(path) {
  const file = fileURLToPath(new URL(path, webRoot));
  if (statSync(file).isDirectory()) {
    for (const name of readdirSync(file).sort()) if (!name.startsWith('.')) include(`${path}/${name}`);
  } else fingerprint.update(path).update(readFileSync(file));
}
for (const path of ['apps/studio/src', 'apps/studio/static', 'packages/ui/src', 'apps/studio/svelte.config.js', 'apps/studio/vite.config.ts', 'package-lock.json']) include(path);
const version = fingerprint.digest('hex').slice(0, 16);

export default {
  preprocess: vitePreprocess(),
  kit: {
    version: { name: version },
    adapter: adapter({
      pages: '../../../src/whoopy/webui/static',
      assets: '../../../src/whoopy/webui/static',
      fallback: 'index.html',
      precompress: false,
      strict: true
    })
  }
};

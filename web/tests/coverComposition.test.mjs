import test from 'node:test';
import assert from 'node:assert/strict';
import { coverThemes, coverCollections, coverTheme, inferCoverMood, coverPoseForMood } from '../apps/studio/src/lib/libraryPresentation.ts';
import { coverPlacements, placeCoverArt, coverArtScale, seatedCoverArt, relaxedCoverArt } from '../apps/studio/src/lib/coverComposition.ts';
import { customCatPoseBounds } from '../apps/studio/src/lib/customCats.ts';
import { catPortraits } from '../apps/studio/src/lib/catCharacters.ts';

test('every preset pose stays on its scene surface and inside the full cover', () => {
  for (const theme of coverThemes) for (const character of Object.keys(seatedCoverArt)) for (const pose of ['seated', 'resting', 'sleeping']) {
    const context = `${theme}/${character}/${pose}`;
    const placement = coverPlacements[theme];
    const source = pose === 'seated' ? seatedCoverArt[character] : relaxedCoverArt[character][pose];
    const art = placeCoverArt(source, pose, placement);
    const scale = art.width / source.source[0];
    const [left, top, right, bottom] = source.bounds;
    assert.ok(Math.abs(art.height / source.source[1] - scale) < 1e-10, `${context}: original aspect ratio`);
    assert.ok(Math.abs(art.y + bottom * scale - placement.baseline) < 1e-8, `${context}: feet touch surface`);
    assert.ok(art.x + left * scale >= placement.surface[0], `${context}: left side supported`);
    assert.ok(art.x + right * scale <= placement.surface[1], `${context}: right side supported`);
    assert.ok(art.y + top * scale >= 12, `${context}: ears have breathing room`);
    assert.ok(art.y + bottom * scale < 180, `${context}: paws fully framed`);
  }
});

test('every custom coat preserves its renderer baseline on every scene', () => {
  for (const theme of coverThemes) for (const pattern of ['solid', 'tabby', 'bicolor', 'tuxedo', 'calico']) for (const pose of ['seated', 'resting', 'sleeping']) {
    const placement = coverPlacements[theme];
    const bounds = customCatPoseBounds(pattern, pose);
    const scale = coverArtScale(bounds.width, bounds.height, pose, placement);
    const y = placement.baseline - bounds.baseline * scale;
    assert.ok(y + (bounds.baseline - bounds.height) * scale >= 12, `${theme}/${pattern}/${pose}: visible ears in frame`);
    assert.ok(placement.x - bounds.width * scale / 2 >= placement.surface[0]);
    assert.ok(placement.x + bounds.width * scale / 2 <= placement.surface[1]);
    assert.ok(Math.abs(y + bounds.baseline * scale - placement.baseline) < 1e-8);
  }
});

test('asymmetric transparent padding does not displace the visible cat', () => {
  const p = coverPlacements.reading;
  const art = placeCoverArt({ source: [1000, 800], bounds: [200, 100, 800, 600] }, 'resting', p);
  const scale = art.width / 1000;
  assert.equal(art.x + 500 * scale, p.x);
  assert.equal(art.y + 600 * scale, p.baseline);
});

test('scene IDs, mood collections and seed choices stay stable', () => {
  assert.equal(coverThemes.length, 17);
  assert.deepEqual(coverCollections.map(c => c.id), ['all', 'sleep', 'grounding', 'focused_attention', 'self_compassion']);
  assert.deepEqual(coverCollections[0].themes, coverThemes);
  for (const collection of coverCollections) {
    assert.ok(collection.themes.length > 0);
    for (const theme of collection.themes) assert.ok(coverThemes.includes(theme));
    for (const seed of ['a', 'course-29', 'my evening practice']) {
      const chosen = coverTheme(seed, collection.id);
      assert.ok(collection.themes.includes(chosen));
      assert.equal(chosen, coverTheme(seed, collection.id));
    }
  }
  assert.ok(new Set(Object.values(coverPlacements).map(p => p.x)).size > 8, 'subjects are composed for their own locations');
});

test('visual mood suggestions honor explicit choices and recognize old titles', () => {
  assert.equal(inferCoverMood('Sleep well', 'focused_attention'), 'focused_attention');
  assert.equal(inferCoverMood('Evening wind-down'), 'sleep');
  assert.equal(inferCoverMood('Kindness toward yourself'), 'self_compassion');
  assert.equal(inferCoverMood('A moment of focus'), 'focused_attention');
  assert.equal(inferCoverMood('Come back to the present'), 'grounding');
  assert.equal(inferCoverMood('Self-directed practice'), 'grounding');
  assert.equal(coverPoseForMood('sleep'), 'sleeping');
  assert.equal(coverPoseForMood('focused_attention'), 'seated');
  assert.equal(coverPoseForMood('self_compassion'), 'resting');
  assert.equal(coverPoseForMood('grounding'), 'resting');
});

test('rendered covers select a mood pose only when no explicit pose was chosen', async () => {
  const { readFile, writeFile, mkdtemp, rm } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const { pathToFileURL } = await import('node:url');
  const { compile } = await import('svelte/compiler');
  const { render } = await import('svelte/server');
  const folder = await mkdtemp(join(tmpdir(), 'whoopy-cover-poses-'));
  const lib = new URL('../apps/studio/src/lib/', import.meta.url);
  try {
    for (const name of ['CustomCat', 'CoverScenery', 'MeditationCover']) {
      let code = compile(await readFile(new URL(`${name}.svelte`, lib), 'utf8'), { generate: 'server', filename: `${name}.svelte` }).js.code;
      code = code.replaceAll("'svelte/internal/server'", JSON.stringify(import.meta.resolve('svelte/internal/server')));
      code = code.replace(/(['"])\.\/([^'"]+)\1/g, (_match, _quote, file) => JSON.stringify(file.endsWith('.svelte')
        ? pathToFileURL(join(folder, file.replace('.svelte', '.mjs'))).href
        : new URL(`${file}.ts`, lib).href));
      await writeFile(join(folder, `${name}.mjs`), code);
    }
    const { default: Cover } = await import(pathToFileURL(join(folder, 'MeditationCover.mjs')).href);
    for (const [props, expected] of [
      [{ mood: 'sleep' }, '/whoopy-sleeping-v9.png'],
      [{ mood: 'focused_attention' }, catPortraits.whoopy],
      [{ mood: 'sleep', pose: 'seated' }, catPortraits.whoopy],
      [{ mood: 'sleep', pose: 'resting' }, '/whoopy-resting-v9.png'],
      [{ mood: 'grounding', pose: 'sleeping' }, '/whoopy-sleeping-v9.png']
    ]) {
      const html = render(Cover, { props: { seed: 'pose-regression', ...props } }).body;
      assert.ok(html.includes(`href="${expected}"`), JSON.stringify(props));
      assert.ok(html.includes('viewBox="0 0 320 180"'));
      assert.ok(html.includes('preserveAspectRatio="xMidYMid meet"'));
    }
  } finally { await rm(folder, { recursive: true, force: true }); }
});

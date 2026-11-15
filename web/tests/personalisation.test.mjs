import test from 'node:test';
import assert from 'node:assert/strict';
import { sanitizeCustomCat, sanitizeCustomCats, defaultCustomCat } from '../apps/studio/src/lib/customCats.ts';
import { coverThemes, isCoverTheme, coverTheme } from '../apps/studio/src/lib/libraryPresentation.ts';
const cat = (id='luna') => ({...defaultCustomCat,id,name:' Luna '});

test('installed voices keep the four familiar cat identities', async () => {
 const { catForVoice, voiceCharacterLabel } = await import('../apps/studio/src/lib/catCharacters.ts');
 for (const [voice, character, label] of [['af_bella', 'whoopy', 'Whoopy'], ['af_heart', 'ginny', 'Ginny'], ['am_michael', 'mogli', 'Mogli'], ['af_nicole', 'mira', 'Mira']]) {
  assert.equal(catForVoice(voice), character);
  assert.equal(voiceCharacterLabel(voice), label);
 }
 assert.equal(voiceCharacterLabel('user-recorded-voice'), 'user-recorded-voice');
});

test('saved companion keeps all supported appearance choices', () => {
 const result=sanitizeCustomCat({...cat(),pattern:'calico',furLength:'fluffy',eyeColor:'#123abc'});
 assert.deepEqual(result,{...cat(),name:'Luna',pattern:'calico',furLength:'fluffy',eyeColor:'#123abc'});
});
test('stored colors, IDs, and enum values cannot become arbitrary SVG input', () => {
 for(const invalid of [{furColor:'url(https://example.com)'},{eyeColor:'red'},{patchColor:'"><script>'},{id:'x"] *'},{pattern:'anything'},{furLength:'huge'},{name:' '},{name:'x'.repeat(33)}]) {
  assert.equal(sanitizeCustomCat({...cat(),...invalid}),null);
 }
 assert.equal(sanitizeCustomCat(null),null);
 assert.deepEqual(sanitizeCustomCats({}),[]);
});
test('malformed saved entries are skipped without losing valid companions', () => {
 assert.deepEqual(sanitizeCustomCats([null,cat('a'),{...cat('b'),eyeColor:'bad'},cat('c')]).map(c=>c.id),['a','c']);
});
test('saved library deduplicates IDs and respects the local studio limit', () => {
 assert.equal(sanitizeCustomCats([cat(),cat()]).length,1);
 assert.equal(sanitizeCustomCats(Array.from({length:40},(_,i)=>cat(`cat-${i}`))).length,24);
});
test('all original scenes migrate and new scenes are selectable', () => {
 for(const id of ['night','dawn','garden','mountain','shore','sunroom']) assert.equal(isCoverTheme(id),true);
 assert.ok(coverThemes.length>=10);
 assert.equal(isCoverTheme('missing-scene'),false);
 assert.equal(coverTheme('same','sleep'),coverTheme('same','sleep'));
});

test('all illustrated coat poses reference real images with matching geometry', async () => {
 const { readFile } = await import('node:fs/promises');
 const { customCatArtwork, customCatPoseBounds } = await import('../apps/studio/src/lib/customCats.ts');
 for (const [pattern, poses] of Object.entries(customCatArtwork)) for (const [pose, art] of Object.entries(poses)) {
  const png=await readFile(new URL(`../apps/studio/static${art.file}`,import.meta.url));
  assert.equal(png.toString('ascii',1,4),'PNG');
  assert.equal(png.readUInt32BE(16),art.width,`${pattern}/${pose}: source width`);
  assert.equal(png.readUInt32BE(20),art.height,`${pattern}/${pose}: source height`);
  const [x,y,width,height]=art.bounds;
  assert.ok(x>=0 && y>=0 && x+width<=art.width && y+height<=art.height,`${pattern}/${pose}: full visible bounds`);
  const placed=customCatPoseBounds(pattern,pose);
  assert.ok(placed.width>0 && placed.width<=253 && placed.height>0 && placed.height<=255);
  assert.equal(placed.baseline,277);
 }
});

test('all coats use exactly the same source anatomy and placement in every pose', async () => {
 const { customCatArtwork, canonicalCatArtwork, customCatPoseBounds } = await import('../apps/studio/src/lib/customCats.ts');
 for (const pose of ['seated', 'resting', 'sleeping']) {
  const expected = customCatPoseBounds('solid', pose);
  for (const pattern of Object.keys(customCatArtwork)) {
   assert.strictEqual(customCatArtwork[pattern][pose], canonicalCatArtwork[pose], `${pattern}/${pose}: identical source object, including facial and paw anchors`);
   assert.deepEqual(customCatPoseBounds(pattern, pose), expected, `${pattern}/${pose}: exact placement, without a scale tolerance`);
  }
 }
});

test('rendered coat variants keep identical source images, transforms and face clips', async () => {
 const { readFile, writeFile, mkdtemp, rm } = await import('node:fs/promises');
 const { join } = await import('node:path');
 const { tmpdir } = await import('node:os');
 const { pathToFileURL } = await import('node:url');
 const { compile } = await import('svelte/compiler');
 const { render } = await import('svelte/server');
 const folder = await mkdtemp(join(tmpdir(), 'whoopy-cat-anatomy-'));
 try {
  const component = await readFile(new URL('../apps/studio/src/lib/CustomCat.svelte', import.meta.url), 'utf8');
  let code = compile(component, { generate: 'server', filename: 'CustomCat.svelte' }).js.code;
  code = code.replaceAll("'svelte/internal/server'", JSON.stringify(import.meta.resolve('svelte/internal/server')))
   .replaceAll("'./customCats'", JSON.stringify(new URL('../apps/studio/src/lib/customCats.ts', import.meta.url).href))
   .replaceAll("'./catCoatPaths'", JSON.stringify(new URL('../apps/studio/src/lib/catCoatPaths.ts', import.meta.url).href));
  const modulePath = join(folder, 'CustomCat.mjs');
  await writeFile(modulePath, code);
  const { default: CustomCat } = await import(pathToFileURL(modulePath).href);
  for (const pose of ['seated', 'resting', 'sleeping']) {
   const output = ['solid', 'tabby', 'bicolor', 'tuxedo', 'calico'].map(pattern => render(CustomCat, { props: { profile: { ...cat(), pattern }, pose } }).body);
   const anatomy = html => ({
    transform: html.match(/<g transform="([^"]+)"/)[1],
    sources: [...html.matchAll(/<image href="([^"]+)"/g)].map(match => match[1]),
    eyes: [...html.matchAll(/<ellipse [^>]+>/g)].map(match => match[0]),
    details: html.match(/<clipPath id="[^"]+-details">([\s\S]*?)<\/clipPath>/)[1]
   });
   const expected = anatomy(output[0]);
   for (const html of output) assert.deepEqual(anatomy(html), expected, `${pose}: pixel-identical anatomy inputs`);
   assert.equal(new Set(expected.sources).size, 1, `${pose}: no alternate pattern PNG can supply a different head or paw`);
  }
 } finally { await rm(folder, { recursive: true, force: true }); }
});

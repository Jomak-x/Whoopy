import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  studioBackgrounds, isStudioBackground, readStudioBackground, readStudioAppearance,
  saveStudioBackground, saveStudioAppearance, STUDIO_BACKGROUND_KEY,
  STUDIO_APPEARANCE_KEY, DEFAULT_STUDIO_BACKGROUND
} from '../apps/studio/src/lib/studioBackgrounds.ts';

function withStorage(run) {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const values = new Map();
  globalThis.window = { localStorage: { getItem:key=>values.get(key)??null, setItem:(key,value)=>values.set(key,value) }, dispatchEvent() {} };
  globalThis.document = { documentElement:{dataset:{}}, body:{dataset:{}} };
  try { run(values); }
  finally {
    if (previousWindow === undefined) delete globalThis.window; else globalThis.window=previousWindow;
    if (previousDocument === undefined) delete globalThis.document; else globalThis.document=previousDocument;
  }
}

test('all saved designs remain available and each has a real light and dark asset', async () => {
  for (const id of ['ink','midnight','daylight','paper','sky','blue-hour','tide','stars','cats','rooftops','aurora','sunplay','clouds','woven']) assert.equal(isStudioBackground(id),true);
  assert.equal(new Set(studioBackgrounds.map(background=>background.id)).size,studioBackgrounds.length);
  assert.equal(isStudioBackground('not-a-canvas'),false);
  for (const {id} of studioBackgrounds) {
    const variants=await Promise.all(['light','dark'].map(mode=>readFile(new URL(`../apps/studio/static/backgrounds/${id}-${mode}.svg`,import.meta.url),'utf8')));
    for (const svg of variants) {
      assert.match(svg, /^<svg[^>]+xmlns="http:\/\/www.w3.org\/2000\/svg"/);
      assert.doesNotMatch(svg, /gradient|foreignObject|<script/i);
    }
    assert.notEqual(variants[0],variants[1],`${id} has deliberately different palettes`);
  }
});

test('old preferences migrate without discarding their design or saved appearance', () => withStorage(values=>{
  assert.equal(readStudioBackground(),DEFAULT_STUDIO_BACKGROUND);
  assert.equal(readStudioAppearance(),'dark');
  values.set(STUDIO_BACKGROUND_KEY,'tide');
  assert.equal(readStudioAppearance(),'light','the old single-mode coast keeps its readable appearance');
  values.set(STUDIO_APPEARANCE_KEY,'dark');
  assert.equal(readStudioAppearance(),'dark','an explicit saved mode is now independent of the design');
  values.set(STUDIO_BACKGROUND_KEY,'obsolete');
  assert.equal(readStudioBackground(),DEFAULT_STUDIO_BACKGROUND);
}));

test('every design survives light-dark switches, reloads, and changes made in another tab', () => withStorage(()=>{
  for (const {id} of studioBackgrounds) for (const mode of ['light','dark']) {
    assert.equal(saveStudioBackground(id,mode),true);
    assert.equal(readStudioBackground(),id);
    assert.equal(readStudioAppearance(),mode);
    assert.equal(document.body.dataset.studioBackground,id);
    assert.equal(document.body.dataset.studioTheme,mode);
    assert.equal(document.documentElement.dataset.studioTheme,mode);
    const other=mode==='light'?'dark':'light';
    assert.equal(saveStudioAppearance(other),true);
    assert.equal(readStudioBackground(),id,'appearance changes the palette, never the artwork');
    assert.equal(readStudioAppearance(),other);
    assert.equal(document.body.dataset.studioBackground,id);
    assert.equal(saveStudioBackground(id),true,'choosing artwork preserves the current mode');
    assert.equal(readStudioAppearance(),other);
  }
}));

test('unavailable storage reports a failed save rather than claiming persistence', () => withStorage(()=>{
  window.localStorage.setItem=()=>{throw new Error('Storage unavailable');};
  assert.equal(saveStudioBackground('cats','light'),false);
  assert.equal(saveStudioAppearance('dark'),false);
}));

test('Walking lines is the default and every stroke continues beyond both screen edges', async () => {
  assert.equal(DEFAULT_STUDIO_BACKGROUND,'contour');
  for (const mode of ['light','dark']) {
    const svg=await readFile(new URL(`../apps/studio/static/backgrounds/contour-${mode}.svg`,import.meta.url),'utf8');
    const lines=[...svg.matchAll(/<path d="([^"]+)" fill="none"/g)];
    assert.ok(lines.length>0);
    for (const [,path] of lines) {
      const values=path.match(/-?\d+(?:\.\d+)?/g).map(Number);
      assert.ok(values[0]<0,'a line must begin outside the left edge');
      assert.ok(values.at(-2)>800,'a line must finish outside the right edge');
    }
  }
});

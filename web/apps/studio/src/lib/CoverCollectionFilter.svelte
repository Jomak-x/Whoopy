<script lang="ts">
  import { coverCollections } from './libraryPresentation';
  import PracticeIllustration from './PracticeIllustration.svelte';

  export let value = 'all';
  export let kind: 'meditation' | 'course' = 'meditation';
  let illustration: 'calm' | 'sleep' | 'focus' | 'kindness' | 'journey' = 'calm';
  $: illustration = kind === 'course' ? 'journey' : value === 'sleep' ? 'sleep' : value === 'focused_attention' ? 'focus' : value === 'self_compassion' ? 'kindness' : 'calm';
</script>

<section class="collection-filter" aria-label="Cover collections">
  <span class="collection-art" aria-hidden="true"><PracticeIllustration kind={illustration} /></span>
  <div><label for="cover-collection">Scene collection</label><select id="cover-collection" bind:value>{#each coverCollections as collection}<option value={collection.id}>{collection.label}</option>{/each}</select><p>{kind === 'course' ? 'A series cover for your course. Choose a setting that fits its theme.' : 'Find a setting for sleep, focus, kindness, or everyday calm.'}</p></div>
</section>

<style>
  .collection-filter { display: flex; align-items: center; gap: 18px; padding: 16px 18px; margin: 6px 0 18px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface-soft); }
  .collection-art { display: block; flex: 0 0 74px; width: 74px; height: 66px; }
  .collection-filter > div { flex: 1; min-width: 0; }
  label { display: block; font-size: 12px; font-weight: 800; color: var(--text); margin-bottom: 7px; }
  select { width: min(100%, 280px); min-height: 40px; border: 1px solid var(--line); border-radius: 7px; color: var(--text); background: var(--surface); padding: 8px 28px 8px 10px; font: inherit; font-size: 12px; }
  p { margin: 8px 0 0; font-size: 11px; line-height: 1.5; color: var(--muted); }
  @media (max-width: 560px) { .collection-filter { gap: 10px; padding: 14px 12px; align-items: flex-start; } .collection-art { flex-basis: 48px; width: 48px; height: 48px; margin-top: 16px; } select { width: 100%; } }
</style>

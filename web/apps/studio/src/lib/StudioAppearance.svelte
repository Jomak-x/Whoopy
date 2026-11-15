<script lang="ts">
  import { onMount } from 'svelte';
  import { readStudioAppearance, saveStudioAppearance, STUDIO_APPEARANCE_EVENT, type StudioAppearanceMode } from '$lib/studioBackgrounds';

  let mode = $state<StudioAppearanceMode>('dark');
  let saveFailed = $state(false);
  onMount(() => {
    const sync = () => { mode = readStudioAppearance(); };
    sync();
    window.addEventListener(STUDIO_APPEARANCE_EVENT, sync);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener(STUDIO_APPEARANCE_EVENT, sync);
      window.removeEventListener('storage', sync);
    };
  });
  function toggle() {
    saveFailed = !saveStudioAppearance(mode === 'dark' ? 'light' : 'dark');
  }
</script>

<button class="appearance-toggle" type="button" role="switch" aria-checked={mode === 'dark'} aria-label="Dark appearance" title={mode === 'dark' ? 'Switch to light appearance' : 'Switch to dark appearance'} onclick={toggle}>
  {#if mode === 'dark'}
    <svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M16.4 12.2a7 7 0 0 1-8.6-8.6 7 7 0 1 0 8.6 8.6Z" /></svg>
  {:else}
    <svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="10" cy="10" r="3.4" /><path d="M10 1.5v2M10 16.5v2M1.5 10h2M16.5 10h2M4 4l1.4 1.4M14.6 14.6 16 16M4 16l1.4-1.4M14.6 5.4 16 4" /></svg>
  {/if}
  <span>{mode === 'dark' ? 'Dark' : 'Light'}</span>
</button>
{#if saveFailed}<span class="appearance-status" role="status">Appearance could not be saved.</span>{/if}

<style>
  .appearance-toggle { display:inline-flex; align-items:center; justify-content:center; gap:7px; min-height:38px; padding:0 10px; border:1px solid var(--line); border-radius:8px; background:var(--surface); color:var(--text); font-size:11px; font-weight:750; white-space:nowrap; }
  .appearance-toggle:hover { background:var(--surface-soft); border-color:var(--muted); }
  .appearance-toggle svg { width:16px; height:16px; stroke:currentColor; stroke-width:1.5; stroke-linecap:round; stroke-linejoin:round; }
  .appearance-status { position:absolute; right:0; top:100%; padding:8px; background:var(--surface); color:var(--danger); font-size:11px; }
  @media(max-width:420px) { .appearance-toggle { padding-inline:8px; gap:5px; } }
</style>

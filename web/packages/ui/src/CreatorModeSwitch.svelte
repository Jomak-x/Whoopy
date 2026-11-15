<script lang="ts">
  import type { CreatorKind } from './types';
  let { mode = $bindable('meditation'), disabled = false }: { mode?: CreatorKind; disabled?: boolean } = $props();
</script>

<div class="creator-mode" role="group" aria-label="Creation type">
  <button type="button" class:active={mode === 'meditation'} aria-pressed={mode === 'meditation'} {disabled} onclick={() => mode = 'meditation'}>Meditation</button>
  <button type="button" class:active={mode === 'course'} aria-pressed={mode === 'course'} {disabled} onclick={() => mode = 'course'}>Course</button>
</div>

<style>
  .creator-mode{display:inline-grid;grid-template-columns:1fr 1fr;gap:4px;padding:4px;border:1px solid var(--whoopy-line);border-radius:12px;background:var(--whoopy-surface)}.creator-mode button{min-height:44px;padding:0 18px;border:0;border-radius:8px;background:transparent;color:var(--whoopy-muted);font:800 13px inherit}.creator-mode button.active{background:var(--whoopy-orange);color:var(--whoopy-navy)}
</style>

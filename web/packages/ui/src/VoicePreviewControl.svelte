<script lang="ts">
  let { available = false, playing = false, onToggle }: { available?: boolean; playing?: boolean; onToggle: () => void } = $props();
</script>

<button type="button" class:playing onclick={onToggle} disabled={!available}>{available ? (playing ? 'Pause' : 'Preview') : 'Preview unavailable'}</button>

<style>
  button{min-width:112px;min-height:44px;padding:0 10px;border:1px solid var(--whoopy-line);border-radius:8px;background:var(--whoopy-navy);color:var(--whoopy-text);font:800 11px inherit}button.playing{border-color:var(--whoopy-orange);color:var(--whoopy-orange-soft)}button:focus-visible{outline:3px solid var(--whoopy-orange);outline-offset:2px}
</style>

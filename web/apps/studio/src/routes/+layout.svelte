<script lang="ts">
  import { onMount } from 'svelte';
  import { applyStudioBackground, readStudioBackground, STUDIO_BACKGROUND_KEY, STUDIO_APPEARANCE_KEY } from '$lib/studioBackgrounds';
  let { children } = $props();
  onMount(() => {
    applyStudioBackground(readStudioBackground());
    const refreshBackground = (event: StorageEvent) => {
      if (event.key === STUDIO_BACKGROUND_KEY || event.key === STUDIO_APPEARANCE_KEY || event.key === null) applyStudioBackground(readStudioBackground());
    };
    window.addEventListener('storage', refreshBackground);
    return () => window.removeEventListener('storage', refreshBackground);
  });
</script>

{@render children()}

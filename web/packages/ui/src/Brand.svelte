<script lang="ts">
  let {
    href = '/',
    mode = 'studio',
    label = 'whoopy',
    asset = '/cat-logo-v5-head-64.png'
  }: { href?: string; mode?: 'studio' | 'developer'; label?: string; asset?: string } = $props();
</script>

<a class="whoopy-brand" class:developer={mode === 'developer'} {href} aria-label={`${label} ${mode === 'developer' ? 'developer' : 'home'}`}>
  <img src={asset} alt="" />
  <span><strong>{label}</strong>{#if mode === 'developer'}<small>Developer mode</small>{/if}</span>
</a>

<style>
  .whoopy-brand{display:inline-flex;align-items:center;gap:10px;color:var(--whoopy-text);text-decoration:none}.whoopy-brand img{width:40px;height:40px;object-fit:contain;filter:drop-shadow(0 3px 0 rgba(8,22,47,.28))}.whoopy-brand span{display:grid;line-height:1}.whoopy-brand strong{font-size:20px;font-weight:900;letter-spacing:-.04em}.whoopy-brand small{margin-top:4px;color:var(--whoopy-muted);font-size:9px;font-weight:800;letter-spacing:.1em;text-transform:uppercase}.whoopy-brand.developer strong{font-size:15px}
</style>

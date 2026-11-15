<script lang="ts">
  let { label = 'Prototype session states' }: { label?: string } = $props();
</script>

<div class="course-statuses" aria-label={label}><span>Queued</span><span>Generating</span><span>Ready</span><span>Needs attention</span></div>

<style>
  .course-statuses{display:flex;flex-wrap:wrap;gap:5px}.course-statuses span{padding:4px 7px;border:1px solid var(--whoopy-line);border-radius:999px;color:var(--whoopy-muted);font-size:9px;font-weight:800}
</style>

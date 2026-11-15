<script lang="ts">
  type OutlineSession = { id: string; title: string };
  let {
    sessions,
    minutes,
    mood,
    onMove,
    onRemove
  }: {
    sessions: OutlineSession[];
    minutes?: number;
    mood?: string;
    onMove: (index: number, direction: -1 | 1) => void;
    onRemove: (id: string) => void;
  } = $props();
</script>

<ol class="course-outline">
  {#each sessions as session, index}
    <li>
      <span>{index + 1}</span>
      <input aria-label={`Session ${index + 1} title`} bind:value={session.title} />
      {#if minutes || mood}<small>{minutes ? `${minutes} min` : ''}{minutes && mood ? ' · ' : ''}{mood || ''}</small>{/if}
      <div>
        <button type="button" aria-label="Move session up" onclick={() => onMove(index, -1)} disabled={index === 0}>↑</button>
        <button type="button" aria-label="Move session down" onclick={() => onMove(index, 1)} disabled={index === sessions.length - 1}>↓</button>
        <button type="button" aria-label="Remove session" onclick={() => onRemove(session.id)}>×</button>
      </div>
    </li>
  {/each}
</ol>

<style>
  .course-outline{max-height:540px;display:grid;gap:0;overflow:auto;margin:18px 0;padding:0;list-style:none}.course-outline li{min-height:58px;display:grid;grid-template-columns:28px minmax(0,1fr) auto auto;align-items:center;gap:8px;border-bottom:1px solid var(--whoopy-line)}.course-outline li>span{color:var(--whoopy-orange-soft);font-size:11px;font-weight:800}.course-outline input{width:100%;height:44px;margin:0;padding:0 7px;border:0;background:transparent;color:var(--whoopy-text);font:800 13px inherit}.course-outline small{color:var(--whoopy-muted);font-size:9px;white-space:nowrap}.course-outline li>div{display:flex;gap:4px}.course-outline button{width:44px;height:44px;padding:0;border:1px solid var(--whoopy-line);border-radius:8px;background:var(--whoopy-surface);color:var(--whoopy-text);font:800 12px inherit}.course-outline button:disabled{opacity:.35}.course-outline button:focus-visible,.course-outline input:focus-visible{outline:3px solid var(--whoopy-orange);outline-offset:2px}@media(max-width:600px){.course-outline li{grid-template-columns:24px minmax(0,1fr);padding:8px 0}.course-outline small,.course-outline li>div{grid-column:2}.course-outline li>div{justify-content:flex-end}}
</style>

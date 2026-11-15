<script lang="ts">
  import CustomCat from './CustomCat.svelte';
  import { defaultCustomCat, sanitizeCustomCat, type CatPose, type CatPattern, type CustomCatProfile } from './customCats';

  export let initial: CustomCatProfile | undefined = undefined;
  export let onSave: (profile: CustomCatProfile) => void;
  export let onCancel: () => void;

  let draft: CustomCatProfile = { ...defaultCustomCat, ...initial };
  let pose: CatPose = 'seated';
  let error = '';
  const patterns: { value: CatPattern; label: string }[] = [
    { value: 'solid', label: 'Solid' }, { value: 'tabby', label: 'Tabby' },
    { value: 'bicolor', label: 'Bicolor' }, { value: 'tuxedo', label: 'Tuxedo' }, { value: 'calico', label: 'Calico' }
  ];
  const furColors = [ ['Ginger', '#c98d61'], ['Cream', '#e9d9c0'], ['Silver', '#a5a3a1'], ['Charcoal', '#4a4549'], ['Chocolate', '#79584c'], ['White', '#f6eee3'] ];
  const patchColors = [ ['Cream', '#f6e8d6'], ['White', '#fff9f1'], ['Warm brown', '#8d5d48'], ['Charcoal', '#4a4549'], ['Ginger', '#d69968'], ['Silver', '#b6b4b4'] ];
  const eyeColors = [ ['Sage', '#91a878'], ['Amber', '#d9a659'], ['Blue', '#83a9c2'], ['Hazel', '#b3a26b'], ['Copper', '#bd7950'] ];
  type ColorField = 'furColor' | 'patchColor' | 'eyeColor';
  function setColor(field: ColorField, value: string) { draft = { ...draft, [field]: value }; }
  function save() {
    const id = draft.id || (globalThis.crypto?.randomUUID?.() ?? `cat-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`);
    const cat = sanitizeCustomCat({ ...draft, id });
    if (!cat) { error = 'Give your cat a name of up to 32 characters.'; return; }
    error = '';
    onSave(cat);
  }
</script>

<div class="creator">
  <div class="creator-head"><div><p class="eyebrow">YOUR COMPANION</p><h2>{initial?.id ? 'Edit your cat' : 'Create your cat'}</h2><p>A familiar face, with a personality of their own.</p></div><button class="close" type="button" aria-label="Close cat creator" onclick={onCancel}>×</button></div>
  <div class="creator-body">
    <div class="preview-column">
      <div class="preview" class:wide-pose={pose !== 'seated'}><span class="preview-label">MADE BY YOU</span><div class="preview-art"><CustomCat profile={draft} {pose} framing="preview" /></div><span class="preview-name">{draft.name.trim() || 'Your companion'}</span></div>
      <div class="pose-picker" aria-label="Preview pose">
        {#each ['seated', 'resting', 'sleeping'] as option}
          <button type="button" class:active={pose === option} aria-pressed={pose === option} onclick={() => pose = option as CatPose}>{option}</button>
        {/each}
      </div>
      <p class="preview-caption">The same little friend, in every quiet moment.</p>
    </div>
    <div class="controls">
      <label class="name-label" for="custom-cat-name">Name</label>
      <input id="custom-cat-name" type="text" maxlength="32" placeholder="Your cat's name" bind:value={draft.name} />
      {#if error}<p class="error" role="alert">{error}</p>{/if}
      <fieldset><legend>Coat pattern</legend><div class="choices">{#each patterns as option}<button type="button" class:active={draft.pattern === option.value} aria-pressed={draft.pattern === option.value} onclick={() => draft.pattern = option.value}>{option.label}</button>{/each}</div></fieldset>
      {#each [{ label: draft.pattern === 'calico' ? 'Patch color' : 'Fur color', field: 'furColor' as ColorField, colors: furColors }, { label: draft.pattern === 'calico' ? 'Base coat & paws' : 'Chest & paws', field: 'patchColor' as ColorField, colors: patchColors }, { label: 'Eye color', field: 'eyeColor' as ColorField, colors: eyeColors }] as group}
        <fieldset disabled={group.field === 'patchColor' && draft.pattern === 'solid'}><legend>{group.label}<span class="selected-color">{group.colors.find((color) => color[1] === draft[group.field].toLowerCase())?.[0] || 'Custom'}</span></legend><div class="swatches">{#each group.colors as color}<button type="button" class:chosen={draft[group.field].toLowerCase() === color[1]} style:--swatch={color[1]} aria-label={`${group.label}: ${color[0]}`} aria-pressed={draft[group.field].toLowerCase() === color[1]} title={color[0]} onclick={() => setColor(group.field, color[1])}></button>{/each}<label class="custom-color" title="Choose a custom color"><span>Custom</span><input type="color" value={draft[group.field]} aria-label={`Custom ${group.label.toLowerCase()}`} oninput={(event) => setColor(group.field, event.currentTarget.value)} /></label></div>{#if group.field === 'patchColor'}<p class="field-hint">{draft.pattern === 'solid' ? 'Choose a patterned coat to add a second color.' : draft.pattern === 'calico' ? 'A lighter base lets the colored patches stand out.' : 'The same chest and paw color in every pose.'}</p>{/if}{#if group.field === 'eyeColor'}<p class="field-hint">{pose === 'sleeping' ? 'Switch to an awake pose to see their eye color.' : 'Eye color applies to seated and resting poses.'}</p>{/if}</fieldset>
      {/each}
    </div>
  </div>
  <div class="actions"><button type="button" class="cancel" onclick={onCancel}>Cancel</button><button type="button" class="save" onclick={save}>{initial?.id ? 'Save changes' : 'Create companion'}</button></div>
</div>

<style>
  .creator { color:var(--text); background:var(--surface); border-radius:25px; padding:28px; width:100%; max-width:850px; box-sizing:border-box; font-family:inherit; }
  .creator * { box-sizing:border-box; }
  .creator-head { display:flex; justify-content:space-between; gap:16px; }
  .creator-head h2 { margin:5px 0 8px; font-size:1.7rem; font-weight:650; letter-spacing:-.045em; line-height:1.15; }
  .creator-head p { margin:0; color:var(--muted); font-size:.87rem; line-height:1.5; }
  .creator-head .eyebrow { font-size:.62rem; font-weight:800; letter-spacing:.18em; color:var(--orange); }
  .close { border:1px solid var(--line); background:transparent; border-radius:50%; width:36px; min-width:36px; height:36px; min-height:36px; padding:0; font-size:1.4rem; color:var(--muted); cursor:pointer; }
  .creator-body { display:grid; grid-template-columns:minmax(230px,1fr) minmax(270px,1fr); gap:30px; margin-top:26px; }
  .preview-column { min-width:0; }
  .preview { position:relative; height:360px; background:#f4ede3; border:1px solid #e1d6c8; border-radius:12px; overflow:hidden; display:flex; align-items:center; justify-content:center; }
  .preview::after { content:''; position:absolute; bottom:52px; width:57%; height:13px; border-radius:50%; background:#b5a391; filter:blur(11px); opacity:.23; }
  .preview-art { position:absolute; z-index:1; width:100%; height:285px; bottom:42px; padding:0 8px; }
  .preview.wide-pose { height:360px; }
  .wide-pose .preview-art { height:250px; }
  .preview-label { position:absolute; top:19px; left:20px; font-size:.57rem; letter-spacing:.18em; font-weight:800; color:#998776; }
  .preview-name { position:absolute; bottom:18px; z-index:2; font-family:Georgia,serif; font-size:1.13rem; color:#655044; font-style:italic; max-width:90%; text-overflow:ellipsis; overflow:hidden; white-space:nowrap; }
  .pose-picker { display:flex; justify-content:center; gap:5px; margin-top:14px; }
  .pose-picker button,.choices button { border:1px solid var(--line); background:transparent; color:var(--text); border-radius:999px; min-height:35px; padding:7px 12px; font:inherit; font-size:.73rem; cursor:pointer; text-transform:capitalize; transition:background .15s,border-color .15s; }
  .pose-picker button.active { background:var(--orange); border-color:var(--orange); color:var(--on-accent); }
  .choices button.active { background:var(--surface-soft); border-color:var(--orange); color:var(--accent-text); }
  .preview-caption { text-align:center; color:var(--muted); font-size:.69rem; line-height:1.5; margin:11px 6px 0; }
  .controls { display:grid; gap:19px; align-content:start; }
  .name-label,legend { font-size:.75rem; font-weight:750; color:var(--text); }
  .name-label { margin-bottom:-12px; }
  input[type=text] { width:100%; min-height:42px; border:1px solid var(--line); border-radius:9px; background:var(--surface-soft); padding:10px 12px; font:inherit; font-size:.87rem; color:var(--text); }
  input[type=text]::placeholder { color:var(--muted); }
  fieldset { border:0; margin:0; padding:0; min-width:0; }
  legend { padding:0; margin-bottom:10px; width:100%; }
  .selected-color { float:right; font-size:.68rem; font-weight:400; color:var(--muted); }
  .choices,.swatches { display:flex; flex-wrap:wrap; gap:7px; }
  .swatches { align-items:center; gap:6px; }
  .swatches button { display:block; flex:0 0 44px; width:44px; height:44px; min-width:44px; min-height:44px; padding:0; border-radius:50%; background:var(--swatch); border:4px solid var(--surface); box-shadow:0 0 0 1px #d9ccc2; cursor:pointer; position:relative; }
  .swatches button.chosen { box-shadow:0 0 0 2px #527b95; }
  .swatches button.chosen::after { content:''; position:absolute; width:5px; height:5px; border-radius:50%; background:#fff; box-shadow:0 0 0 1px #453b4366; left:15px; top:15px; }
  .custom-color { display:flex; flex:0 0 44px; height:44px; flex-direction:column; justify-content:center; align-items:center; gap:0; color:var(--muted); font-size:.6rem; cursor:pointer; }
  .custom-color input { width:24px; height:28px; min-height:28px; padding:0; border:0; background:transparent; cursor:pointer; }
  .field-hint { min-height:30px; margin:9px 0 0; color:var(--muted); font-size:.66rem; line-height:1.5; }
  fieldset:disabled .swatches { opacity:.4; }
  .error { color:#a73c42; font-size:.78rem; margin:0; }
  .actions { position:sticky; bottom:0; z-index:5; background:var(--surface); display:flex; justify-content:flex-end; gap:10px; border-top:1px solid var(--line); margin-top:26px; padding:12px 0; box-shadow:0 -10px 14px var(--surface); }
  .actions button { border-radius:999px; min-height:40px; padding:10px 19px; font:inherit; font-weight:750; font-size:.8rem; cursor:pointer; }
  .cancel { border:1px solid var(--line); background:transparent; color:var(--muted); }
  .save { border:1px solid var(--orange); background:var(--orange); color:var(--on-accent); }
  button:focus-visible,input:focus-visible { outline:3px solid #739bb4; outline-offset:4px; }
  @media(max-width:640px) { .creator { padding:20px; } .creator-body { grid-template-columns:1fr; gap:23px; margin-top:20px; } .preview,.preview.wide-pose { height:285px; } .preview-art { height:225px; bottom:39px; } .wide-pose .preview-art { height:195px; } .preview::after { bottom:53px; } .controls { gap:18px; } .actions { bottom:0; } .swatches { gap:6px; } }
</style>

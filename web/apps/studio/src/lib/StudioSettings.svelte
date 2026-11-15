<script lang="ts">
  import { onMount } from 'svelte';
  import { studioBackgrounds, DEFAULT_STUDIO_BACKGROUND, readStudioBackground, readStudioAppearance, saveStudioBackground, saveStudioAppearance, STUDIO_APPEARANCE_EVENT, type StudioAppearanceMode, type StudioBackgroundId } from './studioBackgrounds';

  let section = 'backgrounds';
  let selected: StudioBackgroundId = DEFAULT_STUDIO_BACKGROUND;
  let appearance: StudioAppearanceMode = 'dark';
  let error = '';
  let notice = '';
  const modes = ['light', 'dark'] as const;
  const groups = [{ id: 'simple', label: 'Simple' }, { id: 'pattern', label: 'Patterns' }, { id: 'illustration', label: 'Places & illustrations' }] as const;
  $: activeBackground = studioBackgrounds.find(background => background.id === selected) ?? studioBackgrounds[0];

  onMount(() => {
    const sync = () => { selected = readStudioBackground(); appearance = readStudioAppearance(); };
    sync();
    window.addEventListener(STUDIO_APPEARANCE_EVENT, sync);
    window.addEventListener('storage', sync);
    return () => { window.removeEventListener(STUDIO_APPEARANCE_EVENT, sync); window.removeEventListener('storage', sync); };
  });

  function chooseBackground(id: StudioBackgroundId) {
    if (saveStudioBackground(id, appearance)) {
      selected = id;
      error = '';
      notice = `${studioBackgrounds.find(background => background.id === id)?.name} selected.`;
    } else error = 'This browser could not save your background. Please try again.';
  }
  function chooseAppearance(mode: StudioAppearanceMode) {
    if (saveStudioAppearance(mode)) {
      appearance = mode;
      error = '';
      notice = `${mode === 'dark' ? 'Dark' : 'Light'} appearance selected.`;
    } else error = 'This browser could not save your appearance. Please try again.';
  }
</script>

<section class="settings-shell" aria-label="Studio settings">
  <aside class="settings-sidebar">
    <h1><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m9.2 3-.6 2.2-2 .9-2-.6-2 3.5 1.6 1.6v2.8l-1.6 1.5 2 3.5 2-.6 2 .9.6 2.3h4l.6-2.3 2-.9 2 .6 2-3.5-1.6-1.5v-2.8l1.6-1.6-2-3.5-2 .6-2-.9-.6-2.2Z"/><circle cx="11.2" cy="12" r="3.2"/></svg>Settings</h1>
    <nav aria-label="Settings categories"><button type="button" class:active={section === 'backgrounds'} aria-current={section === 'backgrounds' ? 'page' : undefined} aria-controls="background-settings" onclick={() => section = 'backgrounds'}><span class="category-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="16" cy="8" r="2"/><path d="m3 17 6-6 6 7 3-3 3 3"/></svg></span>Backgrounds<span class="sidebar-arrow" aria-hidden="true">›</span></button></nav>
  </aside>

  {#if section === 'backgrounds'}
    <div class="settings-content" id="background-settings">
      <header><h2>Backgrounds</h2><p>Choose a background and the appearance that suits you.</p></header>
      <section class="appearance-section" aria-labelledby="appearance-heading"><div><h3 id="appearance-heading">Appearance</h3><p>Light or dark, with the same background.</p></div><div class="appearance-options" aria-label="Appearance">{#each modes as mode}<button class="appearance-option" type="button" aria-pressed={appearance === mode} onclick={() => chooseAppearance(mode)}><span class="appearance-preview studio-background-surface" data-studio-background={selected} data-studio-theme={mode} aria-hidden="true"><span class="mini-window"><i></i><b></b><span></span><span></span></span></span><span class="appearance-label">{mode === 'light' ? 'Light' : 'Dark'}<span class="radio-mark" aria-hidden="true">{appearance === mode ? '✓' : ''}</span></span></button>{/each}</div></section>
      <div class="wallpaper-heading"><div><h3>Background</h3><p>{activeBackground.name}</p></div><span>Saved automatically on this device</span></div>
      {#if error}<p class="save-error" role="alert">{error}</p>{/if}
      <span class="sr-only" role="status" aria-live="polite">{notice}</span>
      {#each groups as group}
        <section class="background-group" aria-labelledby={`background-group-${group.id}`}><h4 id={`background-group-${group.id}`}>{group.label}</h4><div class="background-grid">{#each studioBackgrounds.filter(background => background.category === group.id) as background}<button class="background-choice" class:selected={selected === background.id} type="button" aria-label={`Use ${background.name} background`} aria-pressed={selected === background.id} onclick={() => chooseBackground(background.id)}><span class="background-swatch studio-background-surface" data-studio-background={background.id} data-studio-theme={appearance}><span class="selection-mark" aria-hidden="true">{selected === background.id ? '✓' : ''}</span></span><span class="background-label"><strong>{background.name}</strong>{#if background.id === DEFAULT_STUDIO_BACKGROUND}<small>Default</small>{/if}</span></button>{/each}</div></section>
      {/each}
    </div>
  {/if}
</section>

<style>
  .settings-shell { display:grid; grid-template-columns:225px minmax(0,1fr); min-height:750px; margin-top:30px; border:1px solid var(--line); border-radius:14px; overflow:hidden; background:var(--surface); color:var(--text); }
  .settings-sidebar { padding:30px 18px; border-right:1px solid var(--line); background:var(--surface-soft); }
  .settings-sidebar h1 { display:flex; align-items:center; gap:10px; margin:0 12px 30px; font-size:24px; font-weight:800; letter-spacing:-.035em; }
  .settings-sidebar h1 svg { width:23px; height:23px; stroke:var(--accent-text); stroke-width:1.6; }
  .settings-sidebar nav button { display:flex; align-items:center; gap:10px; width:100%; min-height:46px; padding:9px 12px; border:1px solid transparent; border-radius:8px; background:transparent; color:var(--text); font-size:13px; font-weight:750; text-align:left; }
  .settings-sidebar nav button.active { background:var(--orange); color:var(--on-accent); }
  .category-icon { display:grid; place-items:center; width:25px; height:25px; }
  .category-icon svg { width:22px; height:22px; stroke:currentColor; stroke-width:1.5; stroke-linecap:round; stroke-linejoin:round; }
  .sidebar-arrow { margin-left:auto; font-size:21px; font-weight:400; }
  .settings-content { min-width:0; padding:36px 40px 44px; }
  .settings-content header { padding-bottom:26px; border-bottom:1px solid var(--line); }
  .settings-content h2 { margin:0 0 9px; font-size:31px; line-height:1.1; letter-spacing:-.04em; }
  .settings-content header p,.appearance-section p,.wallpaper-heading p { margin:0; color:var(--muted); font-size:12px; line-height:1.6; }
  .settings-content h3 { margin:0 0 6px; font-size:15px; line-height:1.3; font-weight:800; }
  .appearance-section { display:grid; grid-template-columns:minmax(0,1fr) 300px; align-items:center; gap:25px; padding:26px 0; border-bottom:1px solid var(--line); }
  .appearance-options { display:grid; grid-template-columns:1fr 1fr; gap:13px; }
  .appearance-option { min-width:0; border:0; padding:0; background:transparent; color:var(--text); text-align:left; }
  .appearance-preview { display:flex; align-items:flex-end; justify-content:center; height:85px; padding:14px 18px 0; overflow:hidden; border:1px solid var(--line); border-radius:7px; }
  .appearance-option[aria-pressed='true'] .appearance-preview { outline:2px solid var(--orange); outline-offset:2px; }
  .mini-window { display:grid; grid-template-columns:24px 1fr; grid-template-rows:14px 15px 15px; gap:4px 7px; width:100%; max-width:110px; height:61px; padding:7px; border:1px solid var(--line); border-radius:5px 5px 0 0; background:var(--surface); }
  .mini-window i { grid-row:1/4; background:var(--surface-soft); border-radius:2px; }
  .mini-window b { display:block; width:65%; background:var(--orange); border-radius:2px; }
  .mini-window > span { display:block; background:var(--surface-soft); border-radius:2px; }
  .appearance-label { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:10px 1px 0; font-size:12px; font-weight:750; }
  .radio-mark { display:grid; place-items:center; width:17px; height:17px; border:1px solid var(--line); border-radius:50%; font-size:10px; }
  [aria-pressed='true'] .radio-mark { border-color:var(--orange); background:var(--orange); color:var(--on-accent); }
  .wallpaper-heading { display:flex; align-items:center; justify-content:space-between; gap:20px; padding-top:27px; }
  .wallpaper-heading > span { color:var(--muted); font-size:10px; }
  .background-group { margin-top:25px; }
  .background-group h4 { margin:0 0 13px; font-size:12px; font-weight:800; color:var(--muted); }
  .background-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:19px 17px; }
  .background-choice { min-width:0; padding:0; border:0; background:transparent; color:var(--text); text-align:left; }
  .background-swatch { display:block; position:relative; width:100%; height:116px; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  .background-choice:hover .background-swatch { border-color:var(--accent-text); }
  .background-choice.selected .background-swatch { outline:2px solid var(--orange); outline-offset:2px; }
  .background-swatch[data-studio-background='cats'] { background-size:230px 180px; background-position:left top; }
  .background-swatch[data-studio-background='stars'] { background-position:right center; }
  .selection-mark { display:none; position:absolute; top:8px; right:8px; width:21px; height:21px; border-radius:50%; place-items:center; background:var(--orange); color:var(--on-accent); font-size:12px; font-weight:900; }
  .selected .selection-mark { display:grid; }
  .background-label { display:flex; justify-content:space-between; align-items:center; gap:6px; padding-top:10px; }
  .background-label strong { font-size:12px; font-weight:750; line-height:1.3; }
  .background-label small { color:var(--muted); font-size:9px; }
  .save-error { margin:18px 0 0; color:var(--danger); font-size:12px; }
  button:focus-visible { outline:3px solid var(--focus); outline-offset:5px; }
  .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
  @media(max-width:1100px) { .settings-shell { grid-template-columns:190px minmax(0,1fr); }.settings-content { padding:30px 26px; }.appearance-section { grid-template-columns:1fr; gap:18px; }.appearance-options { max-width:300px; }.background-grid { gap:18px 13px; }.background-swatch { height:100px; } }
  @media(max-width:720px) { .settings-shell { grid-template-columns:1fr; margin-top:20px; min-height:0; }.settings-sidebar { padding:20px; border-right:0; border-bottom:1px solid var(--line); }.settings-sidebar h1 { margin:0 0 16px; font-size:25px; }.settings-sidebar nav { max-width:260px; }.settings-content { padding:25px 20px 32px; }.settings-content h2 { font-size:28px; }.appearance-options { max-width:none; }.wallpaper-heading { align-items:flex-start; flex-direction:column; gap:8px; }.background-grid { grid-template-columns:repeat(2,minmax(0,1fr)); gap:20px 14px; }.background-swatch { height:100px; }.background-label { align-items:flex-start; flex-direction:column; gap:3px; min-height:38px; }.background-label strong { font-size:12px; } }
</style>

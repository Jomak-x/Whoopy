<script lang="ts">
  import MeditationCover from '$lib/MeditationCover.svelte';
  import type { VoiceOption } from '@whoopy/ui';
  import { catForVoice, catNames, voiceCharacterLabel } from '$lib/catCharacters';

  const artScenes = { whoopy:'sunroom', ginny:'shore', mira:'balcony', mogli:'dawn' } as const;
  type StudioVoice = VoiceOption & { key: string };
  type AvailabilityFilter = 'all' | 'preview' | 'ready';

  let {
    options,
    selectedKey,
    previewingKey,
    onChoose,
    onPreview,
    mode = 'page'
  }: {
    options: StudioVoice[];
    selectedKey: string;
    previewingKey: string;
    onChoose: (option: StudioVoice) => void;
    onPreview: (option: StudioVoice) => void;
    mode?: 'page' | 'dialog';
  } = $props();

  let query = $state('');
  let availabilityFilter = $state<AvailabilityFilter>('all');

  const personalities: Record<string, string> = {
    Ryan: 'Clear and reassuring', af_bella: 'Clear and reassuring',
    Vivian: 'Warm and unhurried', af_heart: 'Warm and unhurried',
    Serena: 'Soft and spacious', af_nicole: 'Soft and spacious',
    Dylan: 'Low and grounded', am_michael: 'Low and grounded'
  };
  const practices: Record<string, string> = {
    Ryan: 'Focus · grounding', af_bella: 'Focus · grounding',
    Vivian: 'Self-kindness · reset', af_heart: 'Self-kindness · reset',
    Serena: 'Sleep · body scan', af_nicole: 'Sleep · body scan',
    Dylan: 'Anxiety · breathing', am_michael: 'Anxiety · breathing'
  };
  const providers: Record<string, string> = {
    'qwen3-custom-voice-1.7b': 'Natural',
    'qwen3-voice-design-1.7b': 'Designed',
    'qwen3-base-clone-1.7b': 'Personal',
    'moss-local-v1.5': 'Expressive'
  };
  const swatches = { whoopy: 'clay', ginny: 'sage', mira: 'lavender', mogli: 'sand' };
  const waveform = [7, 12, 18, 10, 22, 15, 9];

  const previewCount = $derived(options.filter((voice) => Boolean(voice.previewUrl)).length);
  const filtered = $derived(options.filter((voice) => {
    if (availabilityFilter === 'preview' && !voice.previewUrl) return false;
    if (availabilityFilter === 'ready' && voice.availability === 'engine-unavailable') return false;
    const terms = `${voiceCharacterLabel(voice.id)} ${voice.id} ${voice.label} ${voice.description} ${personalities[voice.id] || ''} ${practices[voice.id] || ''} ${providers[voice.modelId] || ''}`.toLowerCase();
    return terms.includes(query.trim().toLowerCase());
  }));

  function resetFilters() {
    query = '';
    availabilityFilter = 'all';
  }
</script>

<div class="voice-window" class:dialog={mode === 'dialog'}>
  <div class="listening-note">
    <p>Meet your voices.<span>{previewCount ? `${previewCount} ${previewCount === 1 ? 'voice has a sample' : 'voices have samples'} to explore.` : 'Local samples appear when a voice pack provides them.'}</span></p>
    <span class="local-label"><i></i> Local voices</span>
  </div>

  <div class="voice-toolbar">
    <label class="voice-search">
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"></circle><path d="m15.5 15.5 4.5 4.5"></path></svg>
      <span class="sr-only">Search voices by name, tone, or practice</span>
      <input type="search" bind:value={query} placeholder="Search by name, tone, or practice" />
    </label>
    <label class="voice-filter">
      <span class="sr-only">Filter voices by availability</span>
      <select bind:value={availabilityFilter}>
        <option value="all">All voices</option>
        <option value="preview">With samples</option>
        <option value="ready">Engine available</option>
      </select>
      <svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m4 6 4 4 4-4"></path></svg>
    </label>
  </div>

  <div class="collection-heading">
    <span>{query.trim() || availabilityFilter !== 'all' ? 'Your matches' : 'The collection'}</span>
    <span aria-live="polite">{filtered.length} {filtered.length === 1 ? 'voice' : 'voices'}</span>
  </div>

  <div class="voice-grid">
    {#each filtered as option (option.key)}
      {@const selected = selectedKey === option.key}
      {@const playing = previewingKey === option.key}
      {@const offline = option.availability === 'engine-unavailable'}
      {@const cat = catForVoice(option.id)}
      {@const characterName = voiceCharacterLabel(option.id)}
      <article class="voice-card" class:selected class:playing class:offline>
        <button
          class="voice-choice"
          disabled={offline}
          aria-pressed={selected}
          aria-label={`${characterName}, ${option.id} · ${providers[option.modelId] || 'Local'} voice. ${personalities[option.id] || option.description}.${selected ? ' Selected.' : ''}${offline ? ' Engine offline.' : ''}`}
          onclick={() => { if (!offline) onChoose(option); }}
        >
          <span class="voice-art {swatches[cat]}" aria-hidden="true">
            <span class="art-caption">{cat === 'whoopy' ? 'Your host' : `${catNames[cat]}’s studio`}</span>
            <MeditationCover seed={`voice-${cat}`} character={cat} theme={artScenes[cat]} pose="seated" />
            <span class="studio-sound">
              <svg class="sound-wave" viewBox="0 0 40 24" fill="none">
                {#each waveform as height, index}
                  <path d={`M ${3 + index * 5.5} ${12 - height / 2} v ${height}`} style={`--bar-delay: ${index * -0.08}s`}></path>
                {/each}
              </svg>
              <span>{playing ? 'Playing' : 'Local voice'}</span>
            </span>
            <span class="selection-mark" class:is-selected={selected}>
              {#if selected}<svg viewBox="0 0 16 16" fill="none"><path d="m3.5 8 3 3 6-6"></path></svg>{:else}<svg viewBox="0 0 16 16" fill="none"><path d="M8 3v10M3 8h10"></path></svg>{/if}
            </span>
          </span>
          <span class="voice-copy">
            <span class="voice-title"><strong>{characterName}</strong>{#if selected}<span class="selected-label">Selected</span>{/if}</span>
            <span class="voice-speaker">{option.id} · {providers[option.modelId] || 'Local'}</span>
            <span class="voice-character">{personalities[option.id] || option.description || 'A voice for your everyday practice'}</span>
            <span class="voice-practice">{practices[option.id] || 'Everyday practice'}</span>
          </span>
        </button>
        <div class="voice-card-footer">
          <span class="voice-availability" class:unavailable={offline}>
            <i></i>{offline ? 'Engine offline' : option.previewUrl ? 'Sample available' : 'No sample provided'}
          </span>
          <button
            class="sample-button"
            class:active={playing}
            disabled={!option.previewUrl}
            aria-label={`${playing ? 'Stop' : 'Play'} ${characterName} sample, ${option.id} voice`}
            aria-pressed={playing}
            onclick={() => { if (option.previewUrl) onPreview(option); }}
          >
            {#if playing}<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="4" y="3" width="3" height="10" rx="1"></rect><rect x="9" y="3" width="3" height="10" rx="1"></rect></svg>{:else}<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M5 2.8a.8.8 0 0 1 1.2-.7l7 5.2a.9.9 0 0 1 0 1.4l-7 5.2a.8.8 0 0 1-1.2-.7z"></path></svg>{/if}
            <span>{playing ? 'Stop' : 'Listen'}</span>
          </button>
        </div>
      </article>
    {:else}
      <div class="voice-empty">
        <h3>{options.length ? 'No voices found' : 'No voices available'}</h3>
        <p>{options.length ? 'No voices match these filters. Try another name, tone, or practice.' : 'Voices will appear here when a local voice engine is available.'}</p>
        {#if options.length}<button onclick={resetFilters}>Show all voices <span aria-hidden="true">↗</span></button>{/if}
      </div>
    {/each}
  </div>

  <p class="collection-note">{options.some((voice) => voice.availability === 'preview-unavailable') ? 'Voices without a sample can still be selected when their engine is available.' : 'Choose a voice to use it for your next creation.'}</p>
</div>

<style>
  .voice-window { color: var(--text); min-width: 0; }
  .voice-window *, .voice-window *::before, .voice-window *::after { box-sizing: border-box; }
  button, input, select { font: inherit; color: inherit; }
  button { cursor: pointer; }
  button:focus-visible, input:focus-visible, select:focus-visible { outline: 3px solid var(--orange); outline-offset: 4px; }
  .listening-note { display: flex; align-items: center; gap: 13px; margin-bottom: 30px; }
  .listening-note p { margin: 0; font-size: 14px; font-weight: 750; line-height: 1.55; }
  .listening-note p span { display: block; color: var(--muted); font-size: 12px; font-weight: 500; }
  .local-label { display: inline-flex; flex-shrink: 0; align-items: center; gap: 7px; margin-left: auto; color: var(--muted); font-size: 11px; }
  .local-label i { width: 5px; height: 5px; border-radius: 50%; background: var(--surface); }
  .voice-toolbar { display: flex; gap: 12px; margin-bottom: 29px; }
  .voice-search { display: flex; flex: 1; min-width: 0; align-items: center; gap: 12px; padding: 0 18px; border: 1px solid var(--line); border-radius: 13px; background: var(--surface); }
  .voice-search > svg { flex: 0 0 18px; width: 18px; height: 18px; color: var(--muted); stroke: currentColor; stroke-width: 1.7; }
  .voice-search input { width: 100%; min-width: 0; min-height: 52px; padding: 0; border: 0; background: transparent; font-size: 13px; }
  .voice-search input::placeholder { color: var(--muted); opacity: 1; }
  .voice-filter { position: relative; flex: 0 0 auto; }
  .voice-filter select { width: 100%; min-height: 54px; padding: 0 42px 0 17px; appearance: none; border: 1px solid var(--line); border-radius: 13px; background: var(--surface); font-size: 12px; font-weight: 700; }
  .voice-filter > svg { position: absolute; pointer-events: none; right: 15px; top: 20px; width: 14px; height: 14px; stroke: #9cadc4; stroke-width: 1.5; }
  .collection-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0 0 14px; color: var(--muted); font-size: 11px; letter-spacing: .06em; }
  .collection-heading > span:first-child { text-transform: uppercase; font-weight: 800; }
  .collection-heading > span:last-child { letter-spacing: 0; }
  .voice-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 242px), 1fr)); gap: 20px; }
  .voice-card { min-width: 0; overflow: hidden; border: 1px solid var(--line); border-radius: 14px; background: var(--surface); }
  .voice-card.selected { border-color: var(--orange); box-shadow: 0 0 0 1px var(--orange); }
  .voice-card:has(.voice-choice:hover:not(:disabled)) { border-color: var(--line); }
  .voice-card.selected:has(.voice-choice:hover:not(:disabled)) { border-color: var(--orange); }
  .voice-choice { display: block; width: 100%; min-height: 0; padding: 0; border: 0; border-radius: 13px 13px 0 0; background: transparent; text-align: left; }
  .voice-choice:disabled { cursor: not-allowed; opacity: 1; }
  .voice-choice:focus-visible { position: relative; z-index: 1; outline-offset: -4px; }
  .voice-art { --swatch: #cda58c; --ink: #60473d; position: relative; display: block; height: auto; aspect-ratio: 16 / 9; overflow: hidden; margin: 8px 8px 0; border-radius: 11px; background: var(--swatch); color: var(--ink); isolation: isolate; }
  .voice-art.sage { --swatch: #b7c9e1; --ink: #405777; }
  .voice-art.lavender { --swatch: #b9b7ce; --ink: #57516f; }
  .voice-art.sand { --swatch: #d2c2a3; --ink: #65543f; }
  .art-caption { padding:5px 7px; border-radius:5px; background:#fff9f0dc; color:#394550; position: absolute; z-index: 3; top: 15px; left: 16px; font-size: 9px; font-weight: 850; letter-spacing: .1em; text-transform: uppercase; }
  .studio-sound { padding:4px 7px; border-radius:5px; background:#fff9f0dc; color:#394550; position: absolute; z-index: 2; bottom: 13px; left: 15px; display: flex; align-items: center; gap: 5px; height: 20px; font-size: 8px; font-weight: 750; letter-spacing: .02em; }
  .sound-wave { width: 25px; height: 17px; overflow: visible; }
  .sound-wave path { stroke: currentColor; stroke-width: 2px; stroke-linecap: round; transform-origin: center; }
  .sage .studio-sound, .sand .studio-sound { bottom: 12px; }
  .selection-mark { position: absolute; top: 11px; right: 11px; display: grid; place-items: center; width: 26px; height: 26px; border: 1px solid #08162f24; border-radius: 50%; color: var(--ink); }
  .selection-mark.is-selected { border-color:var(--orange); background:var(--orange); color:var(--on-accent); }
  .selection-mark svg { width: 14px; height: 14px; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
  .voice-copy { display: block; padding: 21px 20px 20px; }
  .voice-title { display: flex; align-items: start; justify-content: space-between; gap: 10px; }
  .voice-title strong { min-width: 0; overflow-wrap: anywhere; font-size: 23px; font-weight: 750; letter-spacing: -.03em; line-height: 1.2; }
  .selected-label { flex-shrink: 0; margin-top: 4px; color: var(--accent-text); font-size: 10px; font-weight: 750; }
  .voice-speaker { display: block; margin-top: 8px; color: var(--muted); font-size: 10px; line-height: 1.5; }
  .voice-character { display: block; min-height: 18px; margin-top: 7px; color: var(--muted); font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
  .voice-practice { display: block; margin-top: 17px; color: var(--text); font-size: 11px; line-height: 1.5; }
  .voice-card-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 0 20px; padding: 12px 0 14px; border-top: 1px solid var(--line); }
  .voice-availability { display: inline-flex; min-width: 0; align-items: center; gap: 6px; color: var(--muted); font-size: 9px; line-height: 1.4; }
  .voice-availability i { flex-shrink: 0; width: 4px; height: 4px; border-radius: 50%; background: var(--muted); }
  .voice-availability.unavailable i { background:var(--accent-text); }
  .sample-button { display: inline-flex; flex-shrink: 0; align-items: center; justify-content: center; gap: 6px; min-width: 73px; min-height: 36px; padding: 5px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-soft); color: var(--text); font-size: 11px; font-weight: 750; }
  .sample-button svg { width: 12px; height: 12px; }
  .sample-button:hover:not(:disabled), .sample-button.active { border-color: var(--orange); color: var(--accent-text); }
  .sample-button:disabled { cursor: not-allowed; opacity: .4; }
  .offline .voice-art { opacity: .6; }
  .offline .voice-copy { opacity: .65; }
  .collection-note { margin: 22px 0 0; color: var(--muted); font-size: 11px; line-height: 1.7; }
  .voice-empty { grid-column: 1 / -1; padding: 55px 24px 60px; border: 1px solid var(--line); border-radius: 14px; text-align: center; background: var(--surface); }
  .voice-empty h3 { margin: 0 0 10px; font-size: 22px; font-weight: 750; letter-spacing: -.025em; }
  .voice-empty p { max-width: 330px; margin: 0 auto; color: var(--muted); font-size: 13px; line-height: 1.7; }
  .voice-empty button { min-height: 44px; margin-top: 20px; padding: 0 15px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface-soft); font-size: 12px; font-weight: 750; }
  .voice-empty button span { margin-left: 12px; }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
  .dialog .voice-grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr)); gap: 16px; }
  .dialog .listening-note { margin-bottom: 24px; }
  .dialog .voice-art { height: auto; }
  @media (prefers-reduced-motion: no-preference) {
    .voice-card { transition: border-color .28s, transform .28s, box-shadow .28s; }
    .voice-card:has(.voice-choice:hover:not(:disabled)) { transform: translateY(-3px); }
    .playing .sound-wave path { animation: sound-pulse 1.1s ease-in-out infinite alternate; animation-delay: var(--bar-delay); }
    @keyframes sound-pulse { from { transform: scaleY(.5); } to { transform: scaleY(1); } }
  }
  @media (max-width: 620px) {
    .voice-toolbar { gap: 8px; margin-bottom: 23px; }
    .voice-search { padding: 0 12px; gap: 8px; }
    .voice-search input { font-size: 12px; }
    .voice-filter select { max-width: 143px; padding-left: 12px; padding-right: 30px; font-size: 11px; }
    .voice-filter > svg { right: 10px; }
    .local-label { display: none; }
    .voice-grid, .dialog .voice-grid { gap: 16px; }
  }
  @media (max-width: 420px) {
    .voice-toolbar { flex-wrap: wrap; }
    .voice-search { flex-basis: 100%; }
    .voice-filter { width: 100%; }
    .voice-filter select { max-width: none; min-height: 44px; }
    .voice-filter > svg { top: 15px; }
    .listening-note { align-items: start; margin-bottom: 22px; gap: 10px; }
    .listening-note p { font-size: 12px; }
    .listening-note p span { font-size: 11px; }
    .voice-art, .dialog .voice-art { height: auto; }
    .voice-title strong { font-size: 22px; }
    .voice-copy { padding: 19px 18px; }
    .voice-card-footer { margin: 0 18px; }
  }
</style>

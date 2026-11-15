<script lang="ts">
  import { coverTheme, coverPoseForMood, isCoverTheme } from './libraryPresentation';
  import { catPortraits, isCatCharacter } from './catCharacters';
  import CustomCat from './CustomCat.svelte';
  import CoverScenery from './CoverScenery.svelte';
  import { coverPlacements, coverArtScale, placeCoverArt, seatedCoverArt, relaxedCoverArt } from './coverComposition';
  import { customCatPoseBounds, type CatPose, type CustomCatProfile } from './customCats';

  export let seed: string;
  export let mood = '';
  export let compact = false;
  export let className = '';
  export let theme: string | undefined = undefined;
  export let character: string = 'whoopy';
  export let pose: string | undefined = undefined;
  let activePose: CatPose = 'seated';
  export let customCat: CustomCatProfile | undefined = undefined;
  export let composition: 'cover' | 'hero' = 'cover';
  export let subject = true;
  export let kind: 'meditation' | 'course' = 'meditation';

  $: scene = isCoverTheme(theme) ? theme : coverTheme(seed, mood);
  $: placement = coverPlacements[scene];
  $: cat = isCatCharacter(character) ? character : 'whoopy';
  $: activePose = pose === 'seated' || pose === 'resting' || pose === 'sleeping' ? pose : coverPoseForMood(mood);
  $: portrait = activePose === 'seated' ? catPortraits[cat]
    : cat === 'whoopy' ? (activePose === 'resting' ? '/whoopy-resting-v9.png' : '/whoopy-sleeping-v9.png')
    : `/${cat}-${activePose}-v3.png`;
  $: art = placeCoverArt(activePose === 'seated' ? seatedCoverArt[cat] : relaxedCoverArt[cat][activePose], activePose, placement);
  // Every custom coat uses its renderer's unchanged 300-unit frame and baseline.
  $: customBounds = customCatPoseBounds(customCat?.pattern ?? 'tabby', activePose);
  $: customScale = coverArtScale(customBounds.width, customBounds.height, activePose, placement);
  $: customSize = 300 * customScale;
  $: customX = placement.x - 150 * customScale;
  $: customY = placement.baseline - customBounds.baseline * customScale;
  $: visibleWidth = customCat ? customBounds.width * customScale : art.visibleWidth;
</script>

<!-- The artwork is decorative; the adjacent title names the meditation. -->
<span class="meditation-cover {className}" class:meditation-cover-compact={compact} class:meditation-cover-hero={composition === 'hero'} style:background={placement.background} aria-hidden="true">
  <svg class="cover-scene" viewBox="0 0 320 180" preserveAspectRatio="xMidYMid meet" focusable="false" xmlns="http://www.w3.org/2000/svg">
    <CoverScenery {scene}/>
    {#if subject}
      <ellipse cx={placement.x} cy={placement.baseline + .3} rx={visibleWidth * .42} ry="2" fill="#463b39" opacity=".17"/>
      {#if customCat}
        <foreignObject x={customX} y={customY} width={customSize} height={customSize}>
          <div class="custom-cat-art"><CustomCat profile={customCat} pose={activePose}/></div>
        </foreignObject>
      {:else}
        <image href={portrait} x={art.x} y={art.y} width={art.width} height={art.height} preserveAspectRatio="xMidYMid meet"/>
      {/if}
    {/if}
    <CoverScenery {scene} foreground/>
    {#if kind === 'course'}
      <!-- A narrow cloth-bound spine and chapter ribbon make a series feel like
           a collected volume. Both live outside every cat's visible bounds. -->
      <path d="M0 0 H10 V180 H0Z" fill="#233b55"/>
      <path d="M7 0 V180" stroke="#d6b279" stroke-width=".8"/>
      <path d="M1 8 H5 M1 12 H5 M1 168 H5 M1 172 H5" stroke="#b5aa90" stroke-width=".8"/>
      <path d="M289 0 H307 V26 L298 21 289 26Z" fill="#f0c17e" stroke="#a77c50" stroke-width=".8"/>
      <path d="M294 7 H302 M294 11 H302 M294 15 H300" stroke="#8e633e" stroke-width="1" stroke-linecap="round"/>
    {/if}
  </svg>
</span>

<style>
  .meditation-cover {
    display: block;
    position: relative;
    overflow: hidden;
    width: 100%;
    height: 100%;
    min-width: 0;
    min-height: 0;
    padding: 0;
    border: 0;
    isolation: isolate;
  }
  .cover-scene { display: block; width: 100%; height: 100%; }
  .custom-cat-art { width: 100%; height: 100%; }
</style>

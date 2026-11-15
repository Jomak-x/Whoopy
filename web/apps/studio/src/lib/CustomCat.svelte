<script lang="ts">
  import { canonicalCatArtwork, customCatPoseBounds, type CatPose, type CustomCatProfile } from './customCats';
  import { catCoatPaths } from './catCoatPaths';
  export let profile: CustomCatProfile;
  export let pose: CatPose = 'seated';
  export let label = '';
  export let framing: 'full' | 'preview' = 'full';

  const id = `cat-paint-${Math.random().toString(36).slice(2, 11)}`;
  // Near-black pigment still needs a little space above the original navy ink.
  function pigment(color: string, floor = .26) {
    const channels = [1, 3, 5].map(offset => parseInt(color.slice(offset, offset + 2), 16) / 255);
    const lift = Math.max(0, floor - Math.max(...channels));
    return `rgb(${channels.map(channel => Math.round((channel + lift) * 255)).join(' ')})`;
  }
  function marking(color: string) {
    const channels = [1, 3, 5].map(offset => parseInt(color.slice(offset, offset + 2), 16));
    const ink = [26, 43, 57];
    return `rgb(${channels.map((channel, index) => Math.round(channel * .22 + ink[index] * .78)).join(' ')})`;
  }
  function iris(color: string) {
    const channels = [1, 3, 5].map(offset => parseInt(color.slice(offset, offset + 2), 16) / 255);
    return channels.map(channel => `${.2126 * channel / .64} ${.7152 * channel / .64} ${.0722 * channel / .64} 0 0`).join(' ') + ' 0 0 0 1 0';
  }
  $: source = canonicalCatArtwork[pose];
  $: paths = catCoatPaths[pose];
  $: geometry = customCatPoseBounds(profile.pattern, pose);
  $: frameTop = framing === 'preview' && pose !== 'seated' ? Math.floor(geometry.baseline - geometry.height - 18) : 0;
  $: scale = geometry.width / source.bounds[2];
  $: transform = `translate(${150 - (source.bounds[0] + source.bounds[2] / 2) * scale} ${geometry.baseline - (source.bounds[1] + source.bounds[3]) * scale}) scale(${scale})`;
</script>

<svg viewBox={`0 ${frameTop} 300 ${300 - frameTop}`} preserveAspectRatio="xMidYMax meet" role="img" aria-label={label || `${profile.name || 'Your cat'}, ${pose}`} xmlns="http://www.w3.org/2000/svg">
  <defs>
    <mask id={`${id}-silhouette`} maskUnits="userSpaceOnUse" x="0" y="0" width={source.width} height={source.height} style="mask-type:alpha">
      <image href={source.file} width={source.width} height={source.height} />
    </mask>
    <!-- A continuous shade layer has opaque alpha. The original silhouette is
         applied once, after paint and ink, so edge pixels cannot turn white. -->
    <filter id={`${id}-shade`} x="0" y="0" width="100%" height="100%" color-interpolation-filters="sRGB">
      <feColorMatrix values=".09567 .32184 .03249 0 .55 .09567 .32184 .03249 0 .55 .09567 .32184 .03249 0 .55 0 0 0 0 1" />
    </filter>
    <!-- Neutral brown and cream have R > B; navy ink has B > R. Smooth
         chroma coverage preserves the source contours without a hard cutoff. -->
    <filter id={`${id}-ink`} x="0" y="0" width="100%" height="100%" color-interpolation-filters="sRGB">
      <feColorMatrix values="1 0 0 0 0 0 1 0 0 0 0 0 1 0 0 -12 0 12 0 0" />
    </filter>
    <!-- The source illustration supplies the chest and paw edges. A colour
         change must not draw a new foreleg or move a fur boundary. -->
    <filter id={`${id}-patch-mask`} x="0" y="0" width="100%" height="100%" color-interpolation-filters="sRGB">
      <feColorMatrix values="0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 7 0 -4.8" />
    </filter>
    <mask id={`${id}-natural-patches`} maskUnits="userSpaceOnUse" x="0" y="0" width={source.width} height={source.height} style="mask-type:alpha">
      <image href={source.file} width={source.width} height={source.height} filter={`url(#${id}-patch-mask)`} />
    </mask>
    <filter id={`${id}-eyes`} color-interpolation-filters="sRGB">
      <feColorMatrix in="SourceGraphic" values="0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 -3 9 -6 0 -.12" result="irisMask" />
      <feColorMatrix in="SourceGraphic" values={iris(profile.eyeColor)} result="irisPaint" />
      <feComposite in="irisPaint" in2="irisMask" operator="in" result="iris" />
      <feComposite in="iris" in2="SourceGraphic" operator="over" />
    </filter>
    <clipPath id={`${id}-eyes-clip`}>{#each source.eyes as eye}<ellipse cx={eye[0]} cy={eye[1]} rx={eye[2]} ry={eye[3]} />{/each}</clipPath>
    <clipPath id={`${id}-details`}><path d={source.details} /><path d={paths.muzzle} /></clipPath>
  </defs>
  <g {transform} data-canonical-pose={pose} data-source={source.file} data-coat={profile.pattern}>
    <g mask={`url(#${id}-silhouette)`} style="isolation:isolate">
      <rect width={source.width} height={source.height} fill={pigment(profile.pattern === 'calico' ? profile.patchColor : profile.furColor)} />
      {#if profile.pattern === 'bicolor'}<path d={paths.bicolor} fill={pigment(profile.patchColor)} />{/if}
      {#if profile.pattern === 'calico'}
        <path d={paths.calico[0]} fill={pigment(profile.furColor)} />
        <path d={paths.calico[1]} fill={marking(profile.furColor)} />
      {/if}
      {#if profile.pattern === 'tabby'}<path d={paths.tabby} fill={marking(profile.furColor)} />{/if}
      {#if profile.pattern !== 'solid'}<rect width={source.width} height={source.height} fill={pigment(profile.patchColor)} mask={`url(#${id}-natural-patches)`} />{/if}
      <image href={source.file} width={source.width} height={source.height} filter={`url(#${id}-shade)`} style="mix-blend-mode:multiply" />
      <image href={source.file} width={source.width} height={source.height} filter={`url(#${id}-ink)`} />
      <!-- The small warm muzzle and its original mouth/chin contours remain
           anatomical features even when the selected chest pigment is black. -->
      <image href={source.file} width={source.width} height={source.height} clip-path={`url(#${id}-details)`} />
      {#if source.eyes.length}<image href={source.file} width={source.width} height={source.height} filter={`url(#${id}-eyes)`} clip-path={`url(#${id}-eyes-clip)`} />{/if}
      <path d={paths.chin} fill="none" stroke="#233747" stroke-width="3.2" stroke-linecap="round" />
      <path d={paths.brows} fill="none" stroke="#fff8ed" stroke-width="2.8" stroke-linecap="round" />
      <path d={paths.whiskers} fill="none" stroke="#fffaf2" stroke-width="3.8" stroke-linecap="round" />
    </g>
  </g>
</svg>

<style>svg { display: block; width: 100%; height: 100%; overflow: visible; }</style>

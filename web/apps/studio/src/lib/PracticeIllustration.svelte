<script lang="ts">
  export let kind: 'calm' | 'sleep' | 'focus' | 'kindness' | 'journey' | 'voices' = 'calm';
</script>

<!-- Small, purpose-built illustrations: a loose thread, a night, a page, a paw. -->
<svg class="practice-illustration" viewBox="0 0 240 180" aria-hidden="true" focusable="false" xmlns="http://www.w3.org/2000/svg">
  {#if kind === 'calm'}
    <ellipse cx="110" cy="150" rx="77" ry="8" fill="#173c6620" />
    <path d="M113 139c36 12 48-20 73-14s15 27 36 15" fill="none" stroke="#e57132" stroke-width="5" stroke-linecap="round" />
    <circle cx="102" cy="100" r="49" fill="#f0934e" />
    <path d="M60 75c16 3 64 20 83 41M55 92c27 4 65 23 77 43M65 65c25 7 58 27 80 49M61 124c18-33 47-56 73-57M77 141c11-37 36-64 66-69" fill="none" stroke="#ffd0a0" stroke-width="4" stroke-linecap="round" />
    <path d="M50 45c6-8 16-9 24-3M151 38c9 3 15 11 15 20" fill="none" stroke="#4f86b7" stroke-width="3" stroke-linecap="round" />
    <circle cx="185" cy="66" r="7" fill="#779fc8" /><circle cx="40" cy="110" r="4" fill="#4a79a9" />
  {:else if kind === 'sleep'}
    <path d="M129 25c-24 40-2 83 44 89-21 30-70 35-98 9s-27-70 1-94c15-13 34-17 53-12Z" fill="#eeb879" />
    <path d="M72 134c22-14 37-9 52 2 19-20 45-21 62-4 16 0 28 12 28 25H37c0-13 13-24 35-23Z" fill="#b4d4ed" />
    <path d="m182 35 3 10 10 3-10 3-3 10-3-10-10-3 10-3Z" fill="#78a8d4" />
    <path d="m39 72 2 7 7 2-7 2-2 7-2-7-7-2 7-2Z" fill="#4d80b0" /><circle cx="200" cy="91" r="3" fill="#4d80b0" />
    <path d="M85 79q7 8 14 0m13 0q7 8 14 0" fill="none" stroke="#6b5949" stroke-width="3" stroke-linecap="round" />
  {:else if kind === 'focus'}
    <ellipse cx="125" cy="155" rx="81" ry="8" fill="#173c6620" />
    <path d="M47 113h145v34H47q-14-17 0-34Z" fill="#4d85bc" /><path d="M53 121h139v18H53q-7-9 0-18Z" fill="#fff2dc" />
    <path d="m60 78 131-15 4 34-131 15q-16-14-4-34Z" fill="#8fb8db" /><path d="m65 86 127-15 2 18-127 15q-8-7-2-18Z" fill="#fff5e4" />
    <path d="m79 50 104 8-2 28-104-8q-12-15 2-28Z" fill="#f19b57" /><path d="m81 57 101 8-1 13-101-8q-6-7 1-13Z" fill="#fff2dc" />
    <path d="m151 59-2 39-10-8-11 7 3-40Z" fill="#d55d34" />
    <path d="M42 45h14M49 38v14M202 111h12M208 105v12" stroke="#729fc9" stroke-width="3" stroke-linecap="round" />
  {:else if kind === 'kindness'}
    <ellipse cx="120" cy="155" rx="67" ry="8" fill="#173c6620" />
    <path d="M79 106c4-21 20-35 41-35s37 14 41 35c25 24 7 51-14 42-18-9-36-9-54 0-21 9-39-18-14-42Z" fill="#8bb8dc" />
    <ellipse cx="62" cy="70" rx="15" ry="22" transform="rotate(-26 62 70)" fill="#8bb8dc" />
    <ellipse cx="100" cy="43" rx="15" ry="22" transform="rotate(-9 100 43)" fill="#8bb8dc" />
    <ellipse cx="142" cy="43" rx="15" ry="22" transform="rotate(9 142 43)" fill="#8bb8dc" />
    <ellipse cx="180" cy="70" rx="15" ry="22" transform="rotate(26 180 70)" fill="#8bb8dc" />
    <path d="M120 125s-25-13-25-26c0-13 18-17 25-5 7-12 25-8 25 5 0 13-25 26-25 26Z" fill="#ed8b46" />
  {:else if kind === 'journey'}
    <path d="M23 149c43 17 28-41 70-37s29-52 73-43 25-29 51-27" fill="none" stroke="#80a9cc" stroke-width="3" stroke-linecap="round" stroke-dasharray="3 8" />
    <g transform="rotate(-10 67 105)"><rect x="38" y="78" width="57" height="65" rx="12" fill="#4d83b6" /><path d="M51 98h28M51 107h22M51 116h26" stroke="#e2edf5" stroke-width="3" stroke-linecap="round" /></g>
    <g transform="rotate(8 128 84)"><rect x="99" y="51" width="57" height="65" rx="12" fill="#9bc2df" /><path d="M113 72h28M113 81h22M113 90h26" stroke="#244b70" stroke-width="3" stroke-linecap="round" /></g>
    <g transform="rotate(-7 188 55)"><rect x="160" y="22" width="57" height="65" rx="12" fill="#f19b57" /><path d="m175 52 8 8 15-17" fill="none" stroke="#7c442d" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" /></g>
    <circle cx="25" cy="150" r="7" fill="#e78b4f" />
  {:else}
    <path d="M25 165V65a45 45 0 0 1 90 0v100M126 165V49a45 45 0 0 1 90 0v116" fill="#b0d1e9" />
    <path d="M14 161h216v10H14Z" fill="#6a96bc" />
    <image href="/whoopy-seated-v7.png" x="18" y="43" width="61" height="122" preserveAspectRatio="xMidYMax meet" />
    <image href="/ginny-seated-v1.png" x="73" y="66" width="50" height="99" preserveAspectRatio="xMidYMax meet" />
    <image href="/mogli-seated-v1.png" x="123" y="43" width="61" height="122" preserveAspectRatio="xMidYMax meet" />
    <image href="/mira-seated-v1.png" x="178" y="66" width="50" height="99" preserveAspectRatio="xMidYMax meet" />
    <path d="M182 21h14M189 14v14M38 30h10M43 25v10" stroke="#f2b77d" stroke-width="3" stroke-linecap="round" />
  {/if}
</svg>

<style>.practice-illustration { display:block; width:100%; height:100%; overflow:visible; }</style>

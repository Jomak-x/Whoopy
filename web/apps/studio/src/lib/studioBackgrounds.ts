export const STUDIO_BACKGROUND_KEY = 'whoopy.studio.background.v1';
export const DEFAULT_STUDIO_BACKGROUND = 'contour';
export const STUDIO_APPEARANCE_KEY = 'whoopy.studio.appearance.v1';
export const STUDIO_APPEARANCE_EVENT = 'whoopy:appearance';
export type StudioAppearanceMode = 'light' | 'dark';
export type BackgroundCategory = 'simple' | 'pattern' | 'illustration';

export const studioBackgrounds = [
  { id: 'ink', name: 'Classic blue', category: 'simple', family: 'The essentials', description: 'Our familiar blue canvas. Orange has room to shine.', detail: 'Solid · signature blue' },
  { id: 'paper', name: 'Drawing paper', category: 'simple', family: 'The essentials', description: 'A fine paper grain with a few flecks of warm ink.', detail: 'Texture · soft grain' },
  { id: 'daylight', name: 'Warm linen', category: 'simple', family: 'The essentials', description: 'A warm, unpatterned canvas for an uncluttered room.', detail: 'Solid · warm neutral' },
  { id: 'midnight', name: 'Slate', category: 'simple', family: 'The essentials', description: 'A quieter shade of blue with a little depth.', detail: 'Solid · slate blue' },
  { id: 'dots', name: 'Dot journal', category: 'simple', family: 'Small details', description: 'Tiny, evenly spaced dots, like the pages of a notebook.', detail: 'Fine pattern · dotted paper' },
  { id: 'blueprint', name: 'Studio notebook', category: 'simple', family: 'Small details', description: 'Fine squared paper with occasional orange registration marks.', detail: 'Fine pattern · graph paper' },
  { id: 'contour', name: 'Walking lines', category: 'pattern', family: 'Lines & rhythm', description: 'Wandering contour lines, drawn without rushing.', detail: 'Linework · topographic' },
  { id: 'blue-hour', name: 'Loose threads', category: 'pattern', family: 'Lines & rhythm', description: 'Long, gently looping lines, like a cat has borrowed the yarn.', detail: 'Linework · flowing thread' },
  { id: 'gingham', name: 'Picnic cloth', category: 'pattern', family: 'Lines & rhythm', description: 'A small woven check in blue with warm orange stitches.', detail: 'Textile · small check' },
  { id: 'arches', name: 'Little arches', category: 'pattern', family: 'Shape & colour', description: 'Rounded tiles with a few orange doorways tucked between them.', detail: 'Geometric · scalloped tiles' },
  { id: 'terrazzo', name: 'Terrazzo', category: 'pattern', family: 'Shape & colour', description: 'Small, irregular pieces of blue, clay and cream.', detail: 'Scattered shapes · stone chips' },
  { id: 'woven', name: 'Woven waves', category: 'pattern', family: 'Shape & colour', description: 'Broad interlaced bands, inspired by a well-loved blanket.', detail: 'Graphic textile · clay & blue' },
  { id: 'cats', name: 'Good company', category: 'pattern', family: 'A little play', description: 'Cats, loose threads and little stars in an illustrated repeat.', detail: 'Illustrated pattern · cats' },
  { id: 'aurora', name: 'Paper ribbons', category: 'illustration', family: 'A little play', description: 'Broad paper curves, with a little orange at the edges.', detail: 'Paper cut · sweeping shapes' },
  { id: 'sunplay', name: 'Sunday shapes', category: 'illustration', family: 'A little play', description: 'A cheerful collage of sun, hills and small printed shapes.', detail: 'Paper collage · sun & hills' },
  { id: 'tide', name: 'Paper coast', category: 'illustration', family: 'A small escape', description: 'A golden sun above layers of cut-paper sea.', detail: 'Landscape · sea & sun' },
  { id: 'rooftops', name: 'City windows', category: 'illustration', family: 'A small escape', description: 'Roof terraces, chimneys and warm windows across the city.', detail: 'Cityscape · rooftops' },
  { id: 'clouds', name: 'Cloud terraces', category: 'illustration', family: 'A small escape', description: 'Layered clouds and a small orange sun above the room.', detail: 'Illustrated sky · clouds' },
  { id: 'sky', name: 'Quiet hills', category: 'illustration', family: 'A small escape', description: 'Distant hills, a winding path and one unhurried sun.', detail: 'Landscape · rolling hills' },
  { id: 'stars', name: 'Cat constellation', category: 'illustration', family: 'A small escape', description: 'A familiar face traced across the sky, with warm little stars.', detail: 'Line illustration · constellation' }
] as const;

export type StudioBackgroundId = typeof studioBackgrounds[number]['id'];

export function isStudioBackground(value: unknown): value is StudioBackgroundId {
  return studioBackgrounds.some((background) => background.id === value);
}

export function readStudioBackground(): StudioBackgroundId {
  if (typeof window === 'undefined') return DEFAULT_STUDIO_BACKGROUND;
  try {
    const saved = window.localStorage.getItem(STUDIO_BACKGROUND_KEY);
    return isStudioBackground(saved) ? saved : DEFAULT_STUDIO_BACKGROUND;
  } catch { return DEFAULT_STUDIO_BACKGROUND; }
}

/** Only used to migrate a preference saved before every design had both modes. */
export function appearanceForBackground(background: StudioBackgroundId): StudioAppearanceMode {
  return ['daylight', 'paper', 'sky', 'tide', 'cats', 'sunplay', 'clouds', 'woven'].includes(background) ? 'light' : 'dark';
}

export function readStudioAppearance(): StudioAppearanceMode {
  if (typeof window === 'undefined') return 'dark';
  try {
    const saved = window.localStorage.getItem(STUDIO_APPEARANCE_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
    return window.localStorage.getItem(STUDIO_BACKGROUND_KEY) ? appearanceForBackground(readStudioBackground()) : 'dark';
  } catch { return 'dark'; }
}

export function applyStudioAppearance(mode: StudioAppearanceMode): void {
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.studioTheme = mode;
    document.body.dataset.studioTheme = mode;
    window.dispatchEvent(new CustomEvent(STUDIO_APPEARANCE_EVENT, { detail: mode }));
  }
}

export function applyStudioBackground(background: StudioBackgroundId, mode = readStudioAppearance()): void {
  if (typeof document !== 'undefined') {
    document.body.dataset.studioBackground = background;
    applyStudioAppearance(mode);
  }
}

/** Design and appearance are independent: a mode switch never replaces the artwork. */
export function saveStudioBackground(background: StudioBackgroundId, mode = readStudioAppearance()): boolean {
  if (typeof window === 'undefined') return false;
  try {
    window.localStorage.setItem(STUDIO_BACKGROUND_KEY, background);
    window.localStorage.setItem(STUDIO_APPEARANCE_KEY, mode);
    applyStudioBackground(background, mode);
    return true;
  } catch { return false; }
}

export function saveStudioAppearance(mode: StudioAppearanceMode): boolean {
  return saveStudioBackground(readStudioBackground(), mode);
}

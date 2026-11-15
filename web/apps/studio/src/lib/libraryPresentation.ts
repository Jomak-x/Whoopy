import type { CatPose } from './customCats';

/** Small presentation helpers shared by the library and its cover illustrations. */
// Keep the original IDs so saved covers retain their scene.
export const coverThemes = ['night', 'dawn', 'garden', 'mountain', 'shore', 'sunroom', 'balcony', 'fireplace', 'reading', 'rooftops', 'dunes', 'harbor', 'canal', 'cafe', 'lighthouse', 'village', 'observatory'] as const;
export type CoverTheme = (typeof coverThemes)[number];
export const coverThemeLabels: Record<CoverTheme, string> = {
  night: 'Moonlit window', dawn: 'Terracotta dawn', garden: 'Lavender garden',
  mountain: 'Alpine lake', shore: 'Sandy shore', sunroom: 'Sunny window',
  balcony: 'Quiet balcony', fireplace: 'By the fireplace', reading: 'Reading nook',
  rooftops: 'Evening rooftops', dunes: 'Starlit dunes', harbor: 'Still harbor',
  canal: 'Canal houses', cafe: 'Rainy café', lighthouse: 'Coastal lighthouse',
  village: 'Snowy village', observatory: 'Hilltop observatory'
};

export const coverCollections: readonly { id: string; label: string; themes: readonly CoverTheme[] }[] = [
  { id: 'all', label: 'All places', themes: coverThemes },
  { id: 'sleep', label: 'Rest & sleep', themes: ['night', 'shore', 'dunes', 'rooftops', 'cafe', 'observatory'] },
  { id: 'grounding', label: 'Feel grounded', themes: ['garden', 'mountain', 'balcony', 'harbor', 'lighthouse', 'village'] },
  { id: 'focused_attention', label: 'Find focus', themes: ['mountain', 'dawn', 'reading', 'canal', 'observatory'] },
  { id: 'self_compassion', label: 'A little kindness', themes: ['sunroom', 'garden', 'fireplace', 'cafe', 'village'] }
];

export function isCoverTheme(value: unknown): value is CoverTheme {
  return typeof value === 'string' && (coverThemes as readonly string[]).includes(value);
}

function hash(value: string): number {
  let result = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    result = Math.imul(result ^ value.charCodeAt(i), 16777619);
  }
  return result >>> 0;
}

/** A stable scene choice; a cat’s pose is always selected independently. */
export function coverTheme(seed: string, mood = ''): CoverTheme {
  const choices = coverCollections.find((collection) => collection.id === mood.toLowerCase())?.themes || coverThemes;
  return choices[hash(seed) % choices.length];
}

/** A visual recommendation for older titles, never a saved classification. */
export function inferCoverMood(title: string, explicitMood = ''): string {
  const canonical = explicitMood.trim().toLowerCase();
  if (coverCollections.some((collection) => collection.id !== 'all' && collection.id === canonical)) return canonical;
  if (/\b(sleep|sleeping|night|evening|wind[\s-]?down|rest|resting|bedtime)\b/i.test(title)) return 'sleep';
  if (/\b(focus|focused|attention|work|study|concentration)\b/i.test(title)) return 'focused_attention';
  if (/\b(kind|kindness|compassion|self[\s-]?compassion|self[\s-]?love|gentleness)\b/i.test(title)) return 'self_compassion';
  return 'grounding';
}

/** Automatic artwork can reflect the practice; an explicit pose stays a choice. */
export function coverPoseForMood(mood: string): CatPose {
  const value = mood.trim().toLowerCase();
  if (value === 'sleep') return 'sleeping';
  return value === 'focused_attention' ? 'seated' : 'resting';
}

/** Clean machine-generated names without recasing a person's natural title. */
export function cleanDisplayTitle(raw: string): string {
  const withoutPrefix = raw.trim().replace(/^(?:render local script|generate local meditation)\s*:\s*/i, '');
  const filenameLike = /\.(?:md|txt)$/i.test(withoutPrefix) || /[_-]/.test(withoutPrefix) && !/\s/.test(withoutPrefix);
  const base = withoutPrefix.replace(/\.(?:md|txt)$/i, '');
  const cleaned = (filenameLike ? base.replace(/[_-]+/g, ' ') : base.replace(/_+/g, ' ')).replace(/\s+/g, ' ').trim();
  if (!cleaned) return 'Untitled meditation';
  return filenameLike ? cleaned.replace(/\b[a-z]/g, (letter) => letter.toUpperCase()) : cleaned;
}

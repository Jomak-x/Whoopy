export type CatPattern = 'solid' | 'tabby' | 'bicolor' | 'tuxedo' | 'calico';
export type CatFurLength = 'short' | 'fluffy';
export type CatPose = 'seated' | 'resting' | 'sleeping';

export interface CustomCatProfile {
  id: string;
  name: string;
  furColor: string;
  patchColor: string;
  eyeColor: string;
  pattern: CatPattern;
  furLength: CatFurLength;
}

export const defaultCustomCat: CustomCatProfile = {
  id: '', name: '', furColor: '#c98d61', patchColor: '#f6e8d6', eyeColor: '#91a878',
  pattern: 'tabby', furLength: 'short'
};

const patterns: CatPattern[] = ['solid', 'tabby', 'bicolor', 'tuxedo', 'calico'];
const lengths: CatFurLength[] = ['short', 'fluffy'];
const hex = /^#[0-9a-fA-F]{6}$/;

/** Reject malformed saved profiles instead of passing untrusted values into the artwork. */
export function sanitizeCustomCat(value: unknown): CustomCatProfile | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const cat = value as Record<string, unknown>;
  if (typeof cat.id !== 'string' || !/^[a-zA-Z0-9_-]{1,80}$/.test(cat.id) ||
      typeof cat.name !== 'string' || !cat.name.trim() || cat.name.trim().length > 32 ||
      typeof cat.furColor !== 'string' || !hex.test(cat.furColor) ||
      typeof cat.patchColor !== 'string' || !hex.test(cat.patchColor) ||
      typeof cat.eyeColor !== 'string' || !hex.test(cat.eyeColor) ||
      !patterns.includes(cat.pattern as CatPattern) || !lengths.includes(cat.furLength as CatFurLength)) return null;
  return { id: cat.id, name: cat.name.trim(), furColor: cat.furColor, patchColor: cat.patchColor,
    eyeColor: cat.eyeColor, pattern: cat.pattern as CatPattern, furLength: cat.furLength as CatFurLength };
}

export function sanitizeCustomCats(value: unknown): CustomCatProfile[] {
  if (!Array.isArray(value)) return [];
  const result: CustomCatProfile[] = [];
  const ids = new Set<string>();
  for (const item of value.slice(0, 100)) {
    const cat = sanitizeCustomCat(item);
    if (cat && !ids.has(cat.id)) { ids.add(cat.id); result.push(cat); }
    if (result.length === 24) break;
  }
  return result;
}

/** Every coat is painted on this one illustration per pose. Geometry is never
 * selected by pattern: saved pattern IDs change pigment paths only. */
export type CustomCatArt = { file: string; width: number; height: number; bounds: [number, number, number, number]; eyes: [number, number, number, number][]; details: string; anchors: { nose: [number, number]; head: [number, number]; frontPaw: [number, number]; tail: [number, number] } };
export const canonicalCatArtwork: Record<CatPose, CustomCatArt> = {
  seated: { file: '/custom-solid-seated-v3.png', width: 1001, height: 1572, bounds: [42, 30, 931, 1512], eyes: [[622, 331, 40, 35], [799, 362, 44, 31]], details: 'M535 26L675 203L547 244Z M818 203L978 83L941 301Z', anchors: { nose: [687, 444], head: [733, 288], frontPaw: [693, 1425], tail: [420, 1474] } },
  resting: { file: '/custom-solid-resting-v3.png', width: 1900, height: 828, bounds: [89, 26, 1722, 771], eyes: [[1310, 270, 35, 29], [1460, 288, 37, 27]], details: 'M1234 40L1338 156L1247 198Z M1490 162L1584 65L1568 228Z', anchors: { nose: [1368, 359], head: [1400, 220], frontPaw: [1609, 747], tail: [368, 743] } },
  sleeping: { file: '/custom-solid-sleeping-v3.png', width: 1858, height: 846, bounds: [93, 146, 1698, 619], eyes: [], details: 'M1393 157L1486 310L1355 351Z M1630 359L1771 265L1717 430Z', anchors: { nose: [1487, 561], head: [1522, 440], frontPaw: [1421, 684], tail: [370, 710] } }
};
// Keep the public pattern-keyed lookup compatible with existing covers and saves.
export const customCatArtwork: Record<CatPattern, Record<CatPose, CustomCatArt>> = {
  solid: canonicalCatArtwork, tabby: canonicalCatArtwork, bicolor: canonicalCatArtwork,
  tuxedo: canonicalCatArtwork, calico: canonicalCatArtwork
};

/** Visible geometry after the renderer fits its 300px coordinate space. */
export function customCatPoseBounds(pattern: CatPattern, pose: CatPose) {
  const [, , width, height] = customCatArtwork[pattern][pose].bounds;
  const scale = Math.min(252 / width, 254 / height);
  return { width: width * scale, height: height * scale, baseline: 277 };
}

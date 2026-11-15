import type { CatPose } from './customCats';
import type { CoverTheme } from './libraryPresentation';

/** Contact points belong to the furniture/landscape, rather than to the frame.
 * Both portrait sources use these same visible bounds and contact points. */
export type CoverPlacement = {
  x: number;
  baseline: number;
  seatedHeight: number;
  relaxedWidth: number;
  relaxedHeight: number;
  surface: readonly [number, number];
  background: string;
};

export const coverPlacements: Record<CoverTheme, CoverPlacement> = {
  night:       { x: 190, baseline: 141, seatedHeight: 110, relaxedWidth: 164, relaxedHeight: 78, surface: [91, 290], background: '#23354f' },
  dawn:        { x: 112, baseline: 146, seatedHeight: 108, relaxedWidth: 170, relaxedHeight: 78, surface: [17, 207], background: '#f5c6a6' },
  garden:      { x: 184, baseline: 144, seatedHeight: 105, relaxedWidth: 168, relaxedHeight: 77, surface: [87, 282], background: '#e6edda' },
  mountain:    { x: 116, baseline: 146, seatedHeight: 103, relaxedWidth: 164, relaxedHeight: 76, surface: [25, 207], background: '#c8e5ed' },
  shore:       { x: 190, baseline: 147, seatedHeight: 106, relaxedWidth: 176, relaxedHeight: 80, surface: [91, 290], background: '#f7dfb4' },
  sunroom:     { x: 136, baseline: 142, seatedHeight: 114, relaxedWidth: 174, relaxedHeight: 80, surface: [37, 236], background: '#f7dfae' },
  balcony:     { x: 119, baseline: 144, seatedHeight: 108, relaxedWidth: 172, relaxedHeight: 79, surface: [20, 220], background: '#edc5c2' },
  fireplace:   { x: 211, baseline: 150, seatedHeight: 111, relaxedWidth: 164, relaxedHeight: 77, surface: [121, 302], background: '#334960' },
  reading:     { x: 201, baseline: 145, seatedHeight: 113, relaxedWidth: 158, relaxedHeight: 76, surface: [113, 291], background: '#f3d9a4' },
  rooftops:    { x: 112, baseline: 147, seatedHeight: 107, relaxedWidth: 172, relaxedHeight: 79, surface: [17, 210], background: '#2d3d63' },
  dunes:       { x: 194, baseline: 151, seatedHeight: 105, relaxedWidth: 174, relaxedHeight: 78, surface: [95, 293], background: '#323d65' },
  harbor:      { x: 184, baseline: 146, seatedHeight: 105, relaxedWidth: 170, relaxedHeight: 79, surface: [86, 284], background: '#d5e3e7' },
  canal:       { x: 111, baseline: 146, seatedHeight: 105, relaxedWidth: 170, relaxedHeight: 78, surface: [15, 209], background: '#f6dbb3' },
  cafe:        { x: 182, baseline: 143, seatedHeight: 111, relaxedWidth: 170, relaxedHeight: 78, surface: [84, 280], background: '#b9cdd1' },
  lighthouse:  { x: 194, baseline: 147, seatedHeight: 103, relaxedWidth: 172, relaxedHeight: 79, surface: [96, 294], background: '#d4e6df' },
  village:     { x: 192, baseline: 146, seatedHeight: 108, relaxedWidth: 170, relaxedHeight: 78, surface: [96, 289], background: '#708caa' },
  observatory: { x: 123, baseline: 146, seatedHeight: 105, relaxedWidth: 174, relaxedHeight: 78, surface: [25, 222], background: '#263458' }
};

export type CoverArtBounds = {
  source: readonly [number, number];
  bounds: readonly [number, number, number, number];
};

export function coverArtScale(width: number, height: number, pose: CatPose, placement: CoverPlacement) {
  return pose === 'seated' ? placement.seatedHeight / height
    : Math.min(placement.relaxedWidth / width, placement.relaxedHeight / height);
}

/** Bounds are left/top/right/bottom source pixels; never distort the portrait. */
export function placeCoverArt(art: CoverArtBounds, pose: CatPose, placement: CoverPlacement) {
  const [left, top, right, bottom] = art.bounds;
  const width = right - left;
  const scale = coverArtScale(width, bottom - top, pose, placement);
  return {
    x: placement.x - (left + width / 2) * scale,
    y: placement.baseline - bottom * scale,
    width: art.source[0] * scale,
    height: art.source[1] * scale,
    visibleWidth: width * scale
  };
}

  // Source pixels and opaque bounds keep the actual paws on the shared floor.
export const seatedCoverArt: Record<string, CoverArtBounds> = {
    whoopy: { source: [1001, 1572], bounds: [37, 22, 980, 1543] },
    ginny: { source: [1001, 1571], bounds: [39, 20, 989, 1543] },
    mogli: { source: [1001, 1571], bounds: [26, 19, 979, 1553] },
    mira: { source: [1001, 1572], bounds: [33, 22, 982, 1544] }
  };
export const relaxedCoverArt: Record<string, Record<'resting' | 'sleeping', CoverArtBounds>> = {
    whoopy: {
      resting: { source: [1728, 910], bounds: [133, 61, 1529, 832] },
      sleeping: { source: [1859, 846], bounds: [252, 137, 1620, 742] }
    },
    ginny: {
      resting: { source: [1879, 837], bounds: [38, 24, 1842, 812] },
      sleeping: { source: [1893, 831], bounds: [59, 98, 1836, 763] }
    },
    mogli: {
      resting: { source: [1774, 887], bounds: [73, 44, 1703, 858] },
      sleeping: { source: [1855, 848], bounds: [52, 91, 1809, 785] }
    },
    mira: {
      resting: { source: [1774, 887], bounds: [59, 45, 1719, 847] },
      sleeping: { source: [1840, 855], bounds: [47, 62, 1808, 808] }
    }
  };

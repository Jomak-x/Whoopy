/** The cats who accompany voices and meditation artwork. */
export const catCharacters = ['whoopy', 'ginny', 'mogli', 'mira'] as const;
export type CatCharacter = (typeof catCharacters)[number];

export const catNames: Record<CatCharacter, string> = {
  whoopy: 'Whoopy', ginny: 'Ginny', mogli: 'Mogli', mira: 'Mira'
};

export const catPortraits: Record<CatCharacter, string> = {
  whoopy: '/whoopy-seated-v7.png',
  ginny: '/ginny-seated-v1.png',
  mogli: '/mogli-seated-v1.png',
  mira: '/mira-seated-v1.png'
};

export function isCatCharacter(value: unknown): value is CatCharacter {
  return typeof value === 'string' && (catCharacters as readonly string[]).includes(value);
}

const voiceCats: Record<string, CatCharacter> = {
  ryan: 'whoopy', vivian: 'ginny', dylan: 'mogli', serena: 'mira',
  af_bella: 'whoopy', af_heart: 'ginny', am_michael: 'mogli', af_nicole: 'mira'
};

export function catForVoice(id: string): CatCharacter {
  const known = voiceCats[id.toLowerCase()];
  if (known) return known;
  let hash = 2166136261;
  for (let i = 0; i < id.length; i += 1) hash = Math.imul(hash ^ id.charCodeAt(i), 16777619);
  return catCharacters[(hash >>> 0) % catCharacters.length];
}

export function voiceCharacterLabel(id: string): string {
  const known = voiceCats[id.toLowerCase()];
  return known ? catNames[known] : id;
}

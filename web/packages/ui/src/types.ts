export type CreatorKind = 'meditation' | 'course';

export type CreatorDraft = {
  kind: CreatorKind;
  prompt: string;
  mood: string;
  minutes: number;
  voiceId: string;
  modelId: string;
  delivery: string;
};

export type VoiceOption = {
  id: string;
  label: string;
  description: string;
  modelId: string;
  previewUrl?: string;
  availability: 'available' | 'preview-unavailable' | 'engine-unavailable';
};

export type CourseSessionStatus = 'draft' | 'queued' | 'generating' | 'ready' | 'needs-attention';

export type CourseSession = {
  id: string;
  title: string;
  prompt: string;
  position?: number;
  runId?: string;
  status: CourseSessionStatus;
};

export type CourseDraft = {
  schemaVersion: 2;
  id: string;
  revision: number;
  title: string;
  goal: string;
  preset: string;
  sessionCount: number;
  sessionMinutes: number;
  mood: string;
  voiceKey: string;
  modelId?: string;
  status: 'draft' | 'queue-preview';
  folderId?: string;
  coverTheme?: string;
  coverCharacter?: string;
  coverPose?: string;
  createdAt: string;
  updatedAt: string;
  trashedAt?: string;
  sessions: CourseSession[];
};

export type LocalRunPresentation = {
  coverTheme?: string;
  coverCharacter?: string;
  coverPose?: string;
  title?: string;
  prompt?: string;
  mood?: string;
  voiceKey?: string;
  modelId?: string;
  folderId?: string;
  courseId?: string;
  tags?: string[];
  trashedAt?: string;
};

export type LocalLibraryMetadata = {
  schemaVersion: 2;
  folders: Array<{ id: string; name: string; createdAt: string; trashedAt?: string }>;
  runMeta: Record<string, LocalRunPresentation>;
  courses: CourseDraft[];
};

export type ObservedRunEvent = {
  key: string;
  attemptId?: string;
  stage: string;
  segmentId?: string;
  message: string;
  observedAt: string;
};

export const durableStages = [
  'queued',
  'planning',
  'drafting',
  'compiling',
  'waiting_for_model_slot',
  'model_startup',
  'synthesizing',
  'assembling',
  'encoding',
  'quality_check',
  'completed'
] as const;

export type DurableStage = (typeof durableStages)[number];

export const friendlyStageLabels: Record<DurableStage, string> = {
  queued: 'Queued',
  planning: 'Planning',
  drafting: 'Writing',
  compiling: 'Building timeline',
  waiting_for_model_slot: 'Waiting for voice engine',
  model_startup: 'Starting voice engine',
  synthesizing: 'Creating voice',
  assembling: 'Assembling audio',
  encoding: 'Finishing audio',
  quality_check: 'Checking quality',
  completed: 'Ready'
};

export function normalizeDurableStage(stage?: string | null): DurableStage {
  const normalized = (stage || 'queued').toLowerCase();
  if ((durableStages as readonly string[]).includes(normalized)) return normalized as DurableStage;
  const aliases: Record<string, DurableStage> = {
    request: 'queued',
    plan: 'planning',
    script: 'drafting',
    timeline: 'compiling',
    voice: 'synthesizing',
    rendering: 'synthesizing',
    assembly: 'assembling',
    quality: 'quality_check',
    validation: 'quality_check',
    complete: 'completed'
  };
  return aliases[normalized] || 'queued';
}

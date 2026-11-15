<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { replaceState } from '$app/navigation';
  import MeditationCover from '$lib/MeditationCover.svelte';
  import GenerationProgress from '$lib/GenerationProgress.svelte';
  import VoiceWindow from '$lib/VoiceWindow.svelte';
  import CatCreator from '$lib/CatCreator.svelte';
  import CustomCat from '$lib/CustomCat.svelte';
  import PracticeIllustration from '$lib/PracticeIllustration.svelte';
  import StudioAppearance from '$lib/StudioAppearance.svelte';
  import StudioSettings from '$lib/StudioSettings.svelte';
  import FolderDialog from '$lib/FolderDialog.svelte';
  import CoverCollectionFilter from '$lib/CoverCollectionFilter.svelte';
  import { sanitizeCustomCats, sanitizeCustomCat, type CustomCatProfile, type CatPose } from '$lib/customCats';
  import { catCharacters, catNames, catPortraits, isCatCharacter, catForVoice, voiceCharacterLabel } from '$lib/catCharacters';
  import { cleanDisplayTitle, coverThemes, coverThemeLabels, coverCollections, coverPoseForMood, inferCoverMood, isCoverTheme } from '$lib/libraryPresentation';
  import { Brand, CourseOutline, CreatorModeSwitch, type CourseDraft, type CourseSession, type CourseSessionStatus, type LocalLibraryMetadata as LocalState, type LocalRunPresentation as RunMeta, type VoiceOption as SharedVoiceOption } from '@whoopy/ui';

  type View = 'home' | 'create' | 'voices' | 'library' | 'settings';
  type CreateMode = 'meditation' | 'course';
  type LibraryKind = 'all' | 'meditation' | 'course';
  type LibraryState = 'all' | 'ready' | 'creating' | 'attention' | 'draft';
  type Sort = 'newest' | 'oldest' | 'title' | 'duration';

  type Run = {
    run_id: string;
    title: string;
    status: string;
    tts_model?: string | null;
    voice?: string | null;
    duration_seconds?: number | null;
    has_audio: boolean;
    created_at: string;
    error?: string | null;
    execution?: { message?: string; stage?: string; started_at?: string; current_segment_id?: string } | null;
    recovery?: { speech_segments_total?: number; speech_segments_completed?: number } | null;
  };

  type SpeechModel = {
    studio_eligible?: boolean;
    capabilities?: {
      preset_values?: string[];
      preset_voices?: string[];
      delivery?: { values?: string[]; fallback?: string | null };
      preview_url?: string;
      sample_url?: string;
      voice_preview_urls?: Record<string, string>;
    };
  };

  type StudioVoiceOption = SharedVoiceOption & {
    key: string;
  };

  type Folder = LocalState['folders'][number];
  type LibraryItem = {
    key: string;
    kind: 'meditation' | 'course' | 'folder';
    title: string;
    subtitle: string;
    createdAt: string;
    durationSeconds: number;
    state: Exclude<LibraryState, 'all'>;
    tag?: string;
    folderId?: string;
    run?: Run;
    course?: CourseDraft;
    folder?: Folder;
  };

  const STORAGE_KEY = 'whoopy.studio.library.v2';
  const LEGACY_STORAGE_KEY = 'whoopy.studio.library.v1';
  const CATS_STORAGE_KEY = 'whoopy.studio.cats.v1';
  const COURSE_COUNTS = [5, 10, 20, 30];
  const catPoses: { id: CatPose; label: string }[] = [{ id: 'seated', label: 'Sitting' }, { id: 'resting', label: 'Lounging' }, { id: 'sleeping', label: 'Sleeping' }];
  const coursePresets = [
    { id: 'anxiety', label: 'Calm anxiety', goal: 'Build a kinder response to anxious thoughts', mood: 'grounding' },
    { id: 'sleep', label: 'Sleep better', goal: 'Create a steady evening wind-down practice', mood: 'sleep' },
    { id: 'focus', label: 'Build focus', goal: 'Strengthen calm attention for work and study', mood: 'focused_attention' },
    { id: 'kindness', label: 'Self-kindness', goal: 'Practice meeting difficult moments with compassion', mood: 'self_compassion' },
    { id: 'custom', label: 'Custom', goal: '', mood: 'grounding' }
  ];
  const coursePhases: Record<string, string[]> = {
    anxiety: ['Find safety', 'Know the pattern', 'Soften the body', 'Meet the moment', 'Carry calm forward'],
    sleep: ['Leave the day', 'Settle the senses', 'Quiet the mind', 'Trust the night', 'Return to rest'],
    focus: ['Clear the space', 'Choose an anchor', 'Work with distraction', 'Deepen attention', 'Use focus gently'],
    kindness: ['Notice the voice', 'Make room', 'Offer warmth', 'Repair and reconnect', 'Live with kindness'],
    custom: ['Begin', 'Explore', 'Practice', 'Integrate', 'Continue']
  };
  const sessionPractices = ['Arrive', 'Notice', 'Name', 'Breathe', 'Practice', 'Reflect'];
  const startingIdeas = [
    { label: 'Anxiety reset', prompt: 'Help me settle anxious thoughts and feel steady again.', mood: 'grounding' },
    { label: 'Deep sleep', prompt: 'Help me release the day and drift toward deep sleep.', mood: 'sleep' },
    { label: 'Better focus', prompt: 'Help me clear distractions and return to one thing.', mood: 'focused_attention' },
    { label: 'Self-kindness', prompt: 'Help me respond to myself with more patience and care.', mood: 'self_compassion' }
  ];
  const moods = [
    { id: 'grounding', label: 'Grounded' },
    { id: 'sleep', label: 'Sleepy' },
    { id: 'focused_attention', label: 'Focused' },
    { id: 'self_compassion', label: 'Kind' }
  ];
  const modelLabels: Record<string, string> = {
    'qwen3-custom-voice-1.7b': 'Natural',
    'qwen3-voice-design-1.7b': 'Designed',
    'qwen3-base-clone-1.7b': 'Personal',
    'moss-local-v1.5': 'Expressive'
  };
  const emptyState = (): LocalState => ({ schemaVersion: 2, folders: [], runMeta: {}, courses: [] });

  let view: View = 'home';
  let createMode: CreateMode = 'meditation';
  let input = '';
  let minutes = 5;
  let writingProfile: 'standard' | 'high' = 'high';
  let writingProfiles: ('standard' | 'high')[] = ['high', 'standard'];
  let mood = 'grounding';
  let selectedVoiceKey = '';
  let delivery = 'calm';
  let pace = 'slow';
  let runs: Run[] = [];
  let speechModels: Record<string, SpeechModel> = {};
  let localState: LocalState = emptyState();
  let selectedFolder = 'all';
  let folderEditing: Folder | 'new' | null = null;
  let selectedRun: Run | null = null;
  let currentRun: Run | null = null;
  let creating = false;
  let cancelling = false;
  let connectionLost = false;
  let activeTask = '';
  let generatingRunId = '';
  let generationRun: Run | null = null;
  let pendingPresentation: RunMeta = {};
  let error = '';
  let engineOnline = false;
  let statusLoaded = false;
  let engineError = '';
  let libraryError = '';
  let showAdvanced = false;
  let showCourseAdvanced = false;
  let showVoiceChooser = false;
  let miniAudio: HTMLAudioElement;
  let audioPlaying = false;
  let previewAudio: HTMLAudioElement;
  let voiceDialog: HTMLDivElement;
  let runDialog: HTMLDivElement;
  let coverDialog: HTMLDivElement;
  let coverItem: LibraryItem | null = null;
  let coverDraft = '';
  let coverCharacterDraft = 'whoopy';
  let coverPoseDraft: CatPose = 'seated';
  let coverCollectionDraft = 'all';
  $: visibleCoverThemes = coverCollections.find((collection) => collection.id === coverCollectionDraft)?.themes || coverThemes;
  let customCats: CustomCatProfile[] = [];
  let showCatStudio = false;
  let catEditorOpen = false;
  let catEditing: CustomCatProfile | undefined;
  let catStorageError = '';
  let catEditorReturnFocus: HTMLElement | null = null;
  let modalOpen = false;
  let scrolled = false;
  $: modalOpen = showVoiceChooser || Boolean(selectedRun) || Boolean(coverItem) || showCatStudio || Boolean(folderEditing);
  let returnFocus: HTMLElement | null = null;
  let previewingKey = '';
  let pollHandle: ReturnType<typeof setTimeout> | undefined;

  let availableVoices: StudioVoiceOption[] = [];
  let activeVoice: StudioVoiceOption | undefined;
  $: availableVoices = voiceOptions(speechModels);
  $: activeVoice = availableVoices.find((option) => option.key === selectedVoiceKey) || availableVoices[0];

  let libraryQuery = '';
  let libraryKind: LibraryKind = 'all';
  let libraryState: LibraryState = 'all';
  let librarySort: Sort = 'newest';
  let libraryLayout: 'grid' | 'list' = 'grid';
  let libraryLimit = 12;
  let libraryResults: LibraryItem[] = [];

  // Recompute when source data or any filter changes, including metadata-only edits.
  $: {
    runs; localState; speechModels; selectedFolder; libraryKind; libraryState; libraryQuery; librarySort;
    libraryResults = libraryItems();
  }
  $: {
    selectedFolder; libraryKind; libraryState; libraryQuery; librarySort;
    libraryLimit = 12;
  }

  let courseStep: 'setup' | 'review' = 'setup';
  let courseId = '';
  let courseRevision = 0;
  let courseTitle = coursePresets[0].label;
  let courseGoal = coursePresets[0].goal;
  let coursePreset = 'anxiety';
  let courseCount = 10;
  let courseMinutes = 5;
  let courseMood = 'grounding';
  let courseSessions: CourseSession[] = [];

  async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
    const response = await fetch(`/api/v1${path}`, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || body.error || `Request failed (${response.status})`);
    return body as T;
  }

  function safeLocalPreview(url: unknown): string | undefined {
    return typeof url === 'string' && url.startsWith('/api/v1/') ? url : undefined;
  }

  function voiceOptions(models = speechModels): StudioVoiceOption[] {
    const options: StudioVoiceOption[] = [];
    for (const [model, details] of Object.entries(models)) {
      if (!details.studio_eligible) continue;
      const capabilities = details.capabilities || {};
      const voices = capabilities.preset_values || capabilities.preset_voices || [];
      for (const id of voices) {
        const preview = capabilities.voice_preview_urls?.[id] || capabilities.preview_url || capabilities.sample_url;
        options.push({
          key: `${model}::${id}`,
          id,
          modelId: model,
          label: voiceCharacterLabel(id),
          description: `${modelLabels[model] || 'Local'} · ${deliveryLabel(id)}`,
          previewUrl: safeLocalPreview(preview),
          availability: safeLocalPreview(preview) ? 'available' : 'preview-unavailable'
        });
      }
    }
    if (!options.length) {
      for (const id of ['Ryan', 'Vivian', 'Serena', 'Dylan']) {
        options.push({ key: `qwen3-custom-voice-1.7b::${id}`, id, modelId: 'qwen3-custom-voice-1.7b', label: voiceCharacterLabel(id), description: 'Local voice', availability: 'engine-unavailable' });
      }
    }
    return options;
  }

  function deliveryLabel(name: string) {
    const labels: Record<string, string> = { Ryan: 'steady', af_bella: 'steady', Vivian: 'warm', af_heart: 'warm', Serena: 'soft', af_nicole: 'soft', Dylan: 'grounded', am_michael: 'grounded' };
    return labels[name] || 'calm';
  }

  function selectedVoice(): StudioVoiceOption | undefined {
    return voiceOptions().find((item) => item.key === selectedVoiceKey) || voiceOptions()[0];
  }

  function voiceCanGenerate(option = selectedVoice()) {
    return Boolean(option && option.availability !== 'engine-unavailable');
  }

  function deliveryOptions(modelId = selectedVoice()?.modelId) {
    const deliveryCapabilities = modelId ? speechModels[modelId]?.capabilities?.delivery : undefined;
    return deliveryCapabilities?.values?.length ? deliveryCapabilities.values : ['calm'];
  }

  function reconcileVoice() {
    const options = voiceOptions();
    if (!options.some((item) => item.key === selectedVoiceKey)) selectedVoiceKey = options[0]?.key || '';
    const selected = options.find((item) => item.key === selectedVoiceKey);
    const allowed = deliveryOptions(selected?.modelId);
    if (!allowed.includes(delivery)) delivery = speechModels[selected?.modelId || '']?.capabilities?.delivery?.fallback || allowed[0] || 'calm';
  }

  function cleanTitle(run: Run) {
    const override = localState.runMeta[run.run_id]?.title;
    return override || cleanDisplayTitle(run.title);
  }

  function runCoverMood(run: Run) {
    return inferCoverMood(cleanTitle(run), localState.runMeta[run.run_id]?.mood || '');
  }

  function duration(run: Run) {
    if (!run.duration_seconds) return run.status === 'completed' ? 'Ready' : 'Preparing';
    return `${Math.max(1, Math.round(run.duration_seconds / 60))} min`;
  }

  function dateLabel(value: string) {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? 'Recently' : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function fullDate(value: string) {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? 'Date unavailable' : date.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }

  function moodLabel(value?: string) {
    return moods.find((item) => item.id === value)?.label || 'Uncategorized';
  }

  function membershipLabel(meta: RunMeta) {
    const folder = localState.folders.find((item) => item.id === meta.folderId)?.name;
    const course = localState.courses.find((item) => item.id === meta.courseId)?.title;
    return [folder, course].filter(Boolean).join(' · ') || 'No folder or course';
  }


  function runState(run: Run): 'ready' | 'creating' | 'attention' {
    if (run.status === 'completed' && run.has_audio) return 'ready';
    if (run.status === 'failed' || run.status === 'interrupted') return 'attention';
    return 'creating';
  }

  function record(value: unknown): Record<string, unknown> | null {
    return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
  }

  function repairLocalState(value: unknown): LocalState | null {
    const source = record(value);
    if (!source || (source.schemaVersion !== 1 && source.schemaVersion !== 2)) return null;
    const folders = (Array.isArray(source.folders) ? source.folders : []).flatMap((value) => {
      const item = record(value);
      return item && typeof item.id === 'string' && typeof item.name === 'string'
        ? [{ id: item.id, name: item.name, createdAt: typeof item.createdAt === 'string' ? item.createdAt : new Date().toISOString(), ...(typeof item.trashedAt === 'string' ? { trashedAt: item.trashedAt } : {}) }]
        : [];
    });
    const runMeta = Object.fromEntries(Object.entries(record(source.runMeta) || {}).flatMap(([id, value]) => {
      const item = record(value);
      if (!item) return [];
      return [[id, {
        ...(typeof item.folderId === 'string' ? { folderId: item.folderId } : {}), ...(typeof item.courseId === 'string' ? { courseId: item.courseId } : {}),
        ...(typeof item.title === 'string' ? { title: item.title } : {}), ...(typeof item.prompt === 'string' ? { prompt: item.prompt } : {}),
        ...(isCoverTheme(item.coverTheme) ? { coverTheme: item.coverTheme } : {}), ...(validCharacter(item.coverCharacter) ? { coverCharacter: item.coverCharacter } : {}), ...(validPose(item.coverPose) ? { coverPose: item.coverPose } : {}),
        ...(typeof item.mood === 'string' ? { mood: item.mood } : {}), ...(typeof item.voiceKey === 'string' ? { voiceKey: item.voiceKey } : {}),
        ...(typeof item.modelId === 'string' ? { modelId: item.modelId } : {}), ...(Array.isArray(item.tags) ? { tags: item.tags.filter((tag): tag is string => typeof tag === 'string') } : {}),
        ...(typeof item.trashedAt === 'string' ? { trashedAt: item.trashedAt } : {})
      } satisfies RunMeta]];
    })) as Record<string, RunMeta>;
    const courses = (Array.isArray(source.courses) ? source.courses : []).flatMap((value) => {
      const item = record(value);
      if (!item || (item.schemaVersion !== 1 && item.schemaVersion !== 2) || typeof item.id !== 'string') return [];
      const sessions = (Array.isArray(item.sessions) ? item.sessions : []).flatMap((sessionValue) => {
        const session = record(sessionValue);
        return session && typeof session.id === 'string' && typeof session.title === 'string'
          ? [{ id: session.id, title: session.title, prompt: typeof session.prompt === 'string' ? session.prompt : '', position: typeof session.position === 'number' ? session.position : undefined, status: ['draft', 'queued', 'generating', 'ready', 'needs-attention'].includes(String(session.status)) ? session.status as CourseSessionStatus : 'draft' as const, ...(typeof session.runId === 'string' ? { runId: session.runId } : {}) }]
          : [];
      });
      const now = new Date().toISOString();
      return [{ schemaVersion: 2 as const, id: item.id, ...(isCoverTheme(item.coverTheme) ? { coverTheme: item.coverTheme } : {}), ...(validCharacter(item.coverCharacter) ? { coverCharacter: item.coverCharacter } : {}), ...(validPose(item.coverPose) ? { coverPose: item.coverPose } : {}), revision: typeof item.revision === 'number' ? item.revision : 1, title: typeof item.title === 'string' ? item.title : 'Untitled course', goal: typeof item.goal === 'string' ? item.goal : '', preset: typeof item.preset === 'string' ? item.preset : 'custom', sessionCount: sessions.length, mood: typeof item.mood === 'string' ? item.mood : 'grounding', sessionMinutes: typeof item.sessionMinutes === 'number' ? item.sessionMinutes : 5, voiceKey: typeof item.voiceKey === 'string' ? item.voiceKey : '', ...(typeof item.modelId === 'string' ? { modelId: item.modelId } : {}), status: item.status === 'queue-preview' ? 'queue-preview' as const : 'draft' as const, sessions, ...(typeof item.folderId === 'string' ? { folderId: item.folderId } : {}), createdAt: typeof item.createdAt === 'string' ? item.createdAt : now, updatedAt: typeof item.updatedAt === 'string' ? item.updatedAt : now, ...(typeof item.trashedAt === 'string' ? { trashedAt: item.trashedAt } : {}) }];
    });
    return { schemaVersion: 2, folders, runMeta, courses };
  }

  function loadLocalState() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY) || localStorage.getItem(LEGACY_STORAGE_KEY);
      if (saved) {
        const repaired = repairLocalState(JSON.parse(saved));
        if (repaired) {
          localState = repaired;
          persistLocalState(repaired);
          return;
        }
      }
      migrateLegacyFolders();
    } catch {
      localState = emptyState();
    }
  }

  function migrateLegacyFolders() {
    const legacyFolders = JSON.parse(localStorage.getItem('whoopy.folders') || '[]') as string[];
    const legacyMap = JSON.parse(localStorage.getItem('whoopy.folderMap') || '{}') as Record<string, string>;
    const now = new Date().toISOString();
    const folders = legacyFolders.filter((name) => name !== 'All meditations').map((name, index) => ({ id: `legacy-${index}-${slug(name)}`, name, createdAt: now }));
    const ids = Object.fromEntries(folders.map((folder) => [folder.name, folder.id]));
    const runMeta: Record<string, RunMeta> = {};
    for (const [runId, name] of Object.entries(legacyMap)) {
      const folderId = ids[name];
      if (folderId) runMeta[runId] = { folderId };
    }
    localState = { schemaVersion: 2, folders, runMeta, courses: [] };
    persistLocalState();
  }

  function persistLocalState(next = localState) {
    localState = { ...next, folders: [...next.folders], runMeta: { ...next.runMeta }, courses: [...next.courses] };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(localState));
  }

  async function loadStatus() {
    try {
      const status = await api<{ speech_models?: Record<string, SpeechModel>; writing_profiles?: string[] }>('/status');
      if (status.writing_profiles) {
        const supported = status.writing_profiles.filter((profile): profile is 'standard' | 'high' => profile === 'standard' || profile === 'high');
        if (supported.length) {
          writingProfiles = supported;
          if (!supported.includes(writingProfile)) writingProfile = supported[0];
        }
      }
      speechModels = status.speech_models || {};
      engineOnline = true;
      engineError = '';
      reconcileVoice();
    } catch (reason) {
      engineOnline = false;
      engineError = 'The local engine is not available right now.';
      reconcileVoice();
    } finally {
      statusLoaded = true;
    }
  }

  async function loadLibrary() {
    try {
      const library = await api<{ runs: Run[] }>('/runs?audience=studio');
      const loaded = library.runs || [];
      const activePlaceholder = generatingRunId ? runs.find((run) => run.run_id === generatingRunId) : undefined;
      runs = activePlaceholder && !loaded.some((run) => run.run_id === activePlaceholder.run_id) ? [activePlaceholder, ...loaded] : loaded;
      libraryError = '';
    } catch (reason) {
      libraryError = 'The local library could not be loaded right now.';
    }
  }

  async function generate() {
    error = '';
    if (!input.trim()) {
      error = 'Add a few words about what you need.';
      return;
    }
    const chosen = selectedVoice();
    if (!chosen || chosen.availability === 'engine-unavailable') {
      error = 'Choose a voice that is ready in the local engine.';
      return;
    }
    creating = true;
    generationRun = null;
    cancelling = false;
    connectionLost = false;
    const allowedDelivery = deliveryOptions(chosen.modelId);
    if (!allowedDelivery.includes(delivery)) delivery = speechModels[chosen.modelId]?.capabilities?.delivery?.fallback || allowedDelivery[0] || 'calm';
    await tick();
    document.querySelector<HTMLElement>('.working')?.scrollIntoView({ block: 'start', behavior: 'auto' });
    pendingPresentation = { prompt: input.trim(), mood, voiceKey: selectedVoiceKey, modelId: chosen.modelId, coverCharacter: catForVoice(chosen.id), tags: [moodLabel(mood)] };
    try {
      const task = await api<Run & { task_id: string }>('/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: 'prompt', text: input.trim(), minutes, tts_model: chosen.modelId, surface: 'studio', profile: writingProfile,
          writing_method: 'recipe_whole_session', pacing_profile_id: 'meditative-v2', voice: chosen.id, meditation_kind: mood === 'grounding' ? undefined : mood,
          delivery, intensity: 'subtle', pace, qwen_language: 'English',
          ...(chosen.modelId.startsWith('qwen3-custom-voice-') ? { qwen_speaker: chosen.id } : {}),
          ...(chosen.modelId === 'qwen3-voice-design-1.7b' ? { qwen_voice_design_preset: delivery === 'sleepy' ? 'sleep_meditation' : delivery === 'grounded' ? 'grounded_meditation' : 'warm_meditation' } : {})
        })
      });
      generationRun = task;
      activeTask = task.task_id;
      generatingRunId = task.run_id || '';
      if (task.run_id) rememberRunPresentation(task.run_id, pendingPresentation);
      if (task.run_id && !runs.some((run) => run.run_id === task.run_id)) runs = [task, ...runs];
      poll();
    } catch (reason) {
      creating = false;
      error = 'The local engine could not start this meditation. Your choices are still here.';
    }
  }

  function poll() {
    clearTimeout(pollHandle);
    pollHandle = setTimeout(pollTask, 900);
  }

  async function pollTask() {
    try {
      const task = await api<Run>(`/tasks/${activeTask}`);
      generationRun = task;
      generatingRunId = task.run_id || generatingRunId;
      if (task.run_id) rememberRunPresentation(task.run_id, pendingPresentation);
      if (task.run_id) runs = [task, ...runs.filter((run) => run.run_id !== task.run_id)];
      connectionLost = false;
      if (task.status === 'completed') {
        creating = false;
        cancelling = false;
        input = '';
        await loadLibrary();
        const finished = runs.find((run) => run.run_id === task.run_id) || task;
        await playRun(finished, false);
      } else if (task.status === 'failed' || task.status === 'interrupted') {
        creating = false;
        error = cancelling && task.status === 'interrupted'
          ? 'Creation stopped. Your prompt and choices are still here.'
          : 'This meditation could not be finished. Your prompt and choices are still here.';
        cancelling = false;
      } else poll();
    } catch (reason) {
      connectionLost = true;
      error = 'The connection was lost. Your meditation may still be running safely in the background.';
    }
  }

  function rememberRunPresentation(runId: string, meta: RunMeta) {
    persistLocalState({
      ...localState,
      runMeta: { ...localState.runMeta, [runId]: { ...localState.runMeta[runId], ...meta } }
    });
  }

  async function cancelGeneration() {
    if (!generatingRunId || cancelling) return;
    cancelling = true;
    error = '';
    try {
      const task = await api<Run & { task_id?: string }>(`/runs/${generatingRunId}/cancel`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
      });
      activeTask = task.task_id || activeTask;
      poll();
    } catch (reason) {
      cancelling = false;
      error = 'The cancellation request did not reach the local engine. Try the connection again.';
    }
  }

  function retryGenerationConnection() {
    if (!activeTask) return;
    connectionLost = false;
    error = '';
    pollTask();
  }

  async function continueInLibrary() {
    await loadLibrary();
    navigate('library');
  }

  function openFolderDialog(folder?: Folder) {
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    returnFocus = trigger?.closest('details')?.querySelector<HTMLElement>('summary') || trigger;
    dismissMenus();
    folderEditing = folder || 'new';
  }

  function saveFolder(value: string): string | null {
    const name = value.trim();
    if (!name) return 'Enter a folder name.';
    const editingId = folderEditing && folderEditing !== 'new' ? folderEditing.id : null;
    if (localState.folders.some((folder) => !folder.trashedAt && folder.id !== editingId && folder.name.toLocaleLowerCase() === name.toLocaleLowerCase())) {
      return 'A folder with that name already exists.';
    }
    if (editingId) {
      persistLocalState({ ...localState, folders: localState.folders.map((folder) => folder.id === editingId ? { ...folder, name } : folder) });
    } else {
      const folder: Folder = { id: crypto.randomUUID(), name, createdAt: new Date().toISOString() };
      persistLocalState({ ...localState, folders: [...localState.folders, folder] });
      selectedFolder = folder.id;
    }
    closeDialogs();
    return null;
  }

  function renameFolder(folder: Folder) {
    openFolderDialog(folder);
  }

  function trashFolder(folder: Folder) {
    if (!window.confirm(`Move “${folder.name}” to Trash? Its items will stay in All.`)) return;
    const at = new Date().toISOString();
    const runMeta = Object.fromEntries(Object.entries(localState.runMeta).map(([id, meta]) => [id, meta.folderId === folder.id ? { ...meta, folderId: undefined } : meta]));
    const courses = localState.courses.map((course) => course.folderId === folder.id ? { ...course, folderId: undefined } : course);
    persistLocalState({ ...localState, runMeta, courses, folders: localState.folders.map((item) => item.id === folder.id ? { ...item, trashedAt: at } : item) });
    selectedFolder = 'all';
  }

  function moveRun(runId: string, folderId: string) {
    persistLocalState({ ...localState, runMeta: { ...localState.runMeta, [runId]: { ...localState.runMeta[runId], folderId: folderId || undefined } } });
  }

  function moveCourse(courseIdValue: string, folderId: string) {
    persistLocalState({ ...localState, courses: localState.courses.map((course) => course.id === courseIdValue ? { ...course, folderId: folderId || undefined, updatedAt: new Date().toISOString() } : course) });
  }

  function trashItem(item: LibraryItem) {
    const at = new Date().toISOString();
    if (item.run) persistLocalState({ ...localState, runMeta: { ...localState.runMeta, [item.run.run_id]: { ...localState.runMeta[item.run.run_id], trashedAt: at } } });
    if (item.course) persistLocalState({ ...localState, courses: localState.courses.map((course) => course.id === item.course?.id ? { ...course, trashedAt: at, updatedAt: at } : course) });
  }

  function startIdea(idea: (typeof startingIdeas)[number]) {
    input = idea.prompt;
    mood = idea.mood;
    createMode = 'meditation';
    navigate('create');
  }

  function navigate(next: View) {
    if (next !== 'voices') { previewAudio?.pause(); previewingKey = ''; }
    if (next === 'create' && creating) createMode = 'meditation';
    view = next;
    const url = new URL(window.location.href);
    if (next === 'settings') url.searchParams.set('tab', 'settings');
    else url.searchParams.delete('tab');
    replaceState(`${url.pathname}${url.search}${url.hash}`, {});
    tick().then(() => {
      if (next === 'create' && creating) document.querySelector<HTMLElement>('.working')?.scrollIntoView({ block: 'start', behavior: 'instant' });
      else window.scrollTo({ top: 0, behavior: 'instant' });
    });
  }

  function renameRun(run: Run) {
    const title = window.prompt('Rename meditation', cleanTitle(run))?.trim();
    if (!title || title === cleanTitle(run)) return;
    rememberRunPresentation(run.run_id, { title });
  }

  function duplicateRunSettings(run: Run) {
    const meta = localState.runMeta[run.run_id] || {};
    input = meta.prompt || cleanTitle(run);
    mood = meta.mood || 'grounding';
    if (meta.voiceKey && voiceOptions().some((option) => option.key === meta.voiceKey)) selectedVoiceKey = meta.voiceKey;
    minutes = run.duration_seconds ? Math.max(3, Math.min(20, Math.round(run.duration_seconds / 60))) : 5;
    createMode = 'meditation';
    navigate('create');
  }

  function addRunToCourse(run: Run, courseIdValue: string) {
    if (!courseIdValue) return;
    const now = new Date().toISOString();
    persistLocalState({
      ...localState,
      runMeta: { ...localState.runMeta, [run.run_id]: { ...localState.runMeta[run.run_id], courseId: courseIdValue } },
      courses: localState.courses.map((course) => course.id === courseIdValue ? {
        ...course,
        sessions: [...course.sessions, { id: crypto.randomUUID(), title: cleanTitle(run), prompt: localState.runMeta[run.run_id]?.prompt || cleanTitle(run), runId: run.run_id, status: 'ready' }],
        updatedAt: now,
        revision: course.revision + 1
      } : course)
    });
  }

  function restoreItem(item: LibraryItem) {
    if (item.run) persistLocalState({ ...localState, runMeta: { ...localState.runMeta, [item.run.run_id]: { ...localState.runMeta[item.run.run_id], trashedAt: undefined } } });
    if (item.course) persistLocalState({ ...localState, courses: localState.courses.map((course) => course.id === item.course?.id ? { ...course, trashedAt: undefined, updatedAt: new Date().toISOString() } : course) });
    if (item.folder) persistLocalState({ ...localState, folders: localState.folders.map((folder) => folder.id === item.folder?.id ? { ...folder, trashedAt: undefined } : folder) });
  }

  function makeCoursePlan() {
    if (!courseGoal.trim()) return;
    const topic = courseGoal.trim().replace(/[.!?]+$/, '');
    const phases = coursePhases[coursePreset] || coursePhases.custom;
    courseTitle = courseTitle.trim() || `${topic.slice(0, 42)} course`;
    courseSessions = Array.from({ length: courseCount }, (_, index) => ({
      id: crypto.randomUUID(),
      title: `${phases[Math.min(phases.length - 1, Math.floor(index * phases.length / courseCount))]} · ${sessionPractices[index % sessionPractices.length]}`,
      prompt: `${topic}. Session ${index + 1} of ${courseCount}: ${sessionPractices[index % sessionPractices.length].toLowerCase()} within the ${phases[Math.min(phases.length - 1, Math.floor(index * phases.length / courseCount))].toLowerCase()} phase.`,
      status: 'draft'
    }));
    courseStep = 'review';
  }

  function chooseCoursePreset(id: string) {
    const preset = coursePresets.find((item) => item.id === id);
    if (!preset) return;
    coursePreset = id;
    if (id === 'custom') { courseTitle = ''; courseGoal = ''; }
    if (id !== 'custom') {
      courseGoal = preset.goal;
      courseMood = preset.mood;
      courseTitle = preset.label;
    }
  }

  function moveSession(index: number, direction: -1 | 1) {
    const destination = index + direction;
    if (destination < 0 || destination >= courseSessions.length) return;
    const next = [...courseSessions];
    [next[index], next[destination]] = [next[destination], next[index]];
    courseSessions = next;
  }

  function removeSession(id: string) {
    courseSessions = courseSessions.filter((session) => session.id !== id);
  }

  function addSession() {
    courseSessions = [...courseSessions, { id: crypto.randomUUID(), title: 'New session', prompt: courseGoal.trim(), status: 'draft' }];
  }

  function saveCourseDraft() {
    const now = new Date().toISOString();
    const existing = localState.courses.find((course) => course.id === courseId);
    const draft: CourseDraft = {
      schemaVersion: 2,
      id: courseId || crypto.randomUUID(),
      revision: (existing?.revision || courseRevision) + 1,
      title: courseTitle.trim() || 'Untitled course', goal: courseGoal.trim(), mood: courseMood,
      preset: coursePreset,
      sessionCount: courseSessions.length, sessionMinutes: courseMinutes, voiceKey: selectedVoiceKey, modelId: selectedVoice()?.modelId, status: 'draft', sessions: courseSessions,
      folderId: existing?.folderId, coverTheme: existing?.coverTheme, coverCharacter: existing?.coverCharacter, coverPose: existing?.coverPose, createdAt: existing?.createdAt || now, updatedAt: now
    };
    persistLocalState({ ...localState, courses: [...localState.courses.filter((course) => course.id !== draft.id), draft] });
    resetCourse();
    selectedFolder = 'courses';
    navigate('library');
  }

  function editCourse(course: CourseDraft) {
    courseId = course.id;
    courseRevision = course.revision;
    courseTitle = course.title;
    courseGoal = course.goal;
    coursePreset = course.preset || 'custom';
    courseCount = course.sessions.length;
    courseMinutes = course.sessionMinutes;
    courseMood = course.mood;
    courseSessions = course.sessions.map((session) => ({ ...session }));
    selectedVoiceKey = course.voiceKey || selectedVoiceKey;
    courseStep = 'review';
    createMode = 'course';
    navigate('create');
  }

  function resetCourse() {
    courseId = '';
    courseRevision = 0;
    courseTitle = coursePresets[0].label;
    courseGoal = coursePresets[0].goal;
    coursePreset = 'anxiety';
    courseCount = 10;
    courseMinutes = 5;
    courseMood = 'grounding';
    courseSessions = [];
    courseStep = 'setup';
  }

  function libraryItems(): LibraryItem[] {
    const inTrash = selectedFolder === 'trash';
    const runItems: LibraryItem[] = runs
      .filter((run) => Boolean(localState.runMeta[run.run_id]?.trashedAt) === inTrash)
      .map((run) => {
        const meta = localState.runMeta[run.run_id] || {};
        const storedVoice = voiceOptions().find((option) => option.key === meta.voiceKey)?.label;
        return {
          key: `run-${run.run_id}`, kind: 'meditation' as const, title: cleanTitle(run),
          subtitle: `${duration(run)} · ${storedVoice || voiceCharacterLabel(run.voice || 'Local voice')} · ${fullDate(run.created_at)} · ${membershipLabel(meta)}`,
          createdAt: run.created_at, durationSeconds: run.duration_seconds || 0, state: runState(run),
          tag: meta.tags?.[0] || (meta.mood ? moodLabel(meta.mood) : undefined), folderId: meta.folderId, run
        };
      });
    const courseItems: LibraryItem[] = localState.courses
      .filter((course) => Boolean(course.trashedAt) === inTrash)
      .map((course) => ({
        key: `course-${course.id}`, kind: 'course', title: course.title,
        subtitle: `${course.sessions.length} sessions · ${course.sessionMinutes} min each · draft v${course.revision}`,
        createdAt: course.updatedAt, durationSeconds: course.sessions.length * course.sessionMinutes * 60,
        state: 'draft', folderId: course.folderId, course
      }));
    const folderItems: LibraryItem[] = inTrash ? localState.folders.filter((folder) => folder.trashedAt).map((folder) => ({
      key: `folder-${folder.id}`, kind: 'folder', title: folder.name, subtitle: 'Folder', createdAt: folder.trashedAt || folder.createdAt,
      durationSeconds: 0, state: 'draft', folder
    })) : [];
    let items = [...runItems, ...courseItems, ...folderItems];
    if (!inTrash && selectedFolder === 'meditations') items = items.filter((item) => item.kind === 'meditation');
    else if (!inTrash && selectedFolder === 'courses') items = items.filter((item) => item.kind === 'course');
    else if (!inTrash && selectedFolder !== 'all') items = items.filter((item) => item.folderId === selectedFolder);
    if (libraryKind !== 'all') items = items.filter((item) => item.kind === libraryKind);
    if (libraryState !== 'all') items = items.filter((item) => item.state === libraryState);
    const query = libraryQuery.trim().toLowerCase();
    if (query) items = items.filter((item) => `${item.title} ${item.subtitle} ${item.run?.voice || ''}`.toLowerCase().includes(query));
    return items.sort((a, b) => {
      if (librarySort === 'title') return a.title.localeCompare(b.title);
      if (librarySort === 'duration') return b.durationSeconds - a.durationSeconds;
      const delta = new Date(b.createdAt).valueOf() - new Date(a.createdAt).valueOf();
      return librarySort === 'oldest' ? -delta : delta;
    });
  }

  function folderCount(folderId: string) {
    return runs.filter((run) => !localState.runMeta[run.run_id]?.trashedAt && localState.runMeta[run.run_id]?.folderId === folderId).length +
      localState.courses.filter((course) => !course.trashedAt && course.folderId === folderId).length;
  }

  async function playRun(run: Run, autoplay = true) {
    if (!run.has_audio || run.status !== 'completed') {
      openRun(run);
      return;
    }
    currentRun = run;
    await tick();
    if (autoplay) await miniAudio?.play().catch(() => undefined);
  }

  function courseRuns(run: Run) {
    const courseIdValue = localState.runMeta[run.run_id]?.courseId;
    const course = localState.courses.find((item) => item.id === courseIdValue);
    return course?.sessions.map((session) => runs.find((item) => item.run_id === session.runId)).filter((item): item is Run => Boolean(item?.has_audio && item.status === 'completed')) || [];
  }

  function adjacentCourseRun(run: Run, direction: -1 | 1) {
    const playable = courseRuns(run);
    const index = playable.findIndex((item) => item.run_id === run.run_id);
    return index < 0 ? undefined : playable[index + direction];
  }

  async function openRun(run: Run) {
    returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    selectedRun = run;
    await tick();
    runDialog?.focus();
  }

  async function moveDetailSession(direction: -1 | 1) {
    if (!selectedRun) return;
    const adjacent = adjacentCourseRun(selectedRun, direction);
    if (!adjacent) return;
    selectedRun = adjacent;
    await tick();
    runDialog?.focus();
  }

  async function openVoiceChooser() {
    returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    showVoiceChooser = true;
    await tick();
    voiceDialog?.focus();
  }

  async function closeDialogs() {
    folderEditing = null;
    selectedRun = null;
    coverItem = null;
    showCatStudio = false;
    catEditorOpen = false;
    catEditing = undefined;
    showVoiceChooser = false;
    previewAudio?.pause();
    previewingKey = '';
    const target = returnFocus;
    returnFocus = null;
    await tick();
    target?.focus({ preventScroll: true });
  }

  function trapDialogFocus(event: KeyboardEvent) {
    if (event.key !== 'Tab') return;
    const dialog = (coverItem || showCatStudio) ? coverDialog : showVoiceChooser ? voiceDialog : selectedRun ? runDialog : null;
    if (!dialog) return;
    const controls = Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'));
    if (!controls.length) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  function previewVoice(option: StudioVoiceOption) {
    if (!option.previewUrl) return;
    if (previewingKey === option.key && !previewAudio.paused) {
      previewAudio.pause();
      previewingKey = '';
      return;
    }
    previewingKey = option.key;
    previewAudio.src = option.previewUrl;
    previewAudio.play().catch(() => { previewingKey = ''; });
  }

  function chooseVoice(option: StudioVoiceOption) {
    if (option.availability === 'engine-unavailable') return;
    selectedVoiceKey = option.key;
    reconcileVoice();
    if (showVoiceChooser) closeDialogs();
    else { previewAudio?.pause(); previewingKey = ''; }
  }

  function slug(value: string) {
    return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  }

  function dismissMenus(event?: Event) {
    const target = event?.target;
    for (const menu of document.querySelectorAll<HTMLDetailsElement>('details[data-action-menu][open]')) {
      if (!(target instanceof Node) || !menu.contains(target)) menu.open = false;
    }
  }

  function positionActionMenu(event: Event) {
    const menu = event.currentTarget as HTMLDetailsElement;
    if (!menu.open) return;
    dismissMenus(event);
    const trigger = menu.querySelector('summary');
    const panel = menu.querySelector<HTMLElement>(':scope > div');
    if (!trigger || !panel) return;
    const anchor = trigger.getBoundingClientRect();
    const headerBottom = document.querySelector('.topbar')?.getBoundingClientRect().bottom ?? 0;
    const topEdge = Math.max(12, headerBottom + 8);
    const below = window.innerHeight - anchor.bottom - 12;
    const above = anchor.top - topEdge - 8;
    const opensBelow = below >= Math.min(panel.scrollHeight + 2, 330) || below >= above;
    const available = Math.max(80, opensBelow ? below : above);
    const height = Math.min(panel.scrollHeight + 2, 330, available);
    const width = Math.min(panel.getBoundingClientRect().width, window.innerWidth - 24);
    Object.assign(panel.style, {
      position: 'fixed', right: 'auto', bottom: 'auto',
      left: `${Math.max(12, Math.min(anchor.right - width, window.innerWidth - width - 12))}px`,
      top: `${opensBelow ? anchor.bottom + 8 : Math.max(topEdge, anchor.top - height - 8)}px`,
      width: `${width}px`, maxHeight: `${Math.min(330, available)}px`
    });
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      const menu = document.querySelector<HTMLDetailsElement>('details[data-action-menu][open]');
      if (menu) {
        menu.open = false;
        menu.querySelector<HTMLElement>('summary')?.focus();
        event.preventDefault();
        return;
      }
      if (catEditorOpen) { cancelCatEditing(); event.preventDefault(); }
      else if (modalOpen) closeDialogs();
    }
    trapDialogFocus(event);
  }

  function updateScroll() {
    const next = window.scrollY > 20;
    if (next !== scrolled) scrolled = next;
  }

  // Animate visible sections once. Content stays visible if motion or JS is unavailable.
  function reveal(node: HTMLElement) {
    if (typeof IntersectionObserver === 'undefined' || matchMedia('(prefers-reduced-motion: reduce)').matches) return {};
    let animation: Animation | undefined;
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
        animation = node.animate([{ opacity: .55, transform: 'translateY(12px)' }, { opacity: 1, transform: 'translateY(0)' }], { duration: 850, easing: 'cubic-bezier(.2,.7,.2,1)' });
      }
      observer.disconnect();
    }, { threshold: .08 });
    observer.observe(node);
    return { destroy() { observer.disconnect(); animation?.cancel(); } };
  }

  // Opacity avoids creating a containing block for fixed library action menus.
  function viewTransition(node: HTMLElement, currentView: View) {
    let animation: Animation | undefined;
    let previousView = currentView;
    return {
      update(nextView: View) {
        if (nextView === previousView) return;
        previousView = nextView;
        animation?.cancel();
        if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
          animation = node.animate([{ opacity: .2 }, { opacity: 1 }], { duration: 600, easing: 'cubic-bezier(.22,.61,.36,1)' });
        }
      },
      destroy() { animation?.cancel(); }
    };
  }

  function validPose(value: unknown): value is CatPose {
    return value === 'seated' || value === 'resting' || value === 'sleeping';
  }

  function validCharacter(value: unknown): value is string {
    return isCatCharacter(value) || (typeof value === 'string' && /^custom:[a-zA-Z0-9_-]{1,80}$/.test(value));
  }

  function findCustomCat(character: string | undefined, cats: CustomCatProfile[]) {
    return character?.startsWith('custom:') ? cats.find((cat) => cat.id === character.slice(7)) : undefined;
  }

  async function openCatStudio() {
    returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    showCatStudio = true;
    catEditorOpen = false;
    catStorageError = '';
    await tick();
    coverDialog?.focus();
  }

  function startPresetCat(character: (typeof catCharacters)[number]) {
    const looks: Record<string, Partial<CustomCatProfile>> = {
      whoopy: { furColor:'#8e7b60', patchColor:'#f5e8d7', eyeColor:'#a7a268', pattern:'tabby' },
      ginny: { furColor:'#777469', patchColor:'#f8efe3', eyeColor:'#aaaf8d', pattern:'bicolor' },
      mogli: { furColor:'#d59450', patchColor:'#fff3e4', eyeColor:'#cba353', pattern:'bicolor' },
      mira: { furColor:'#292830', patchColor:'#fff4e8', eyeColor:'#c8c666', pattern:'tuxedo' }
    };
    editCat({ id:'', name:'', furColor:'#c98d61', patchColor:'#f6e8d6', eyeColor:'#91a878', pattern:'tabby', furLength:'short', ...looks[character] });
  }

  async function editCat(cat?: CustomCatProfile) {
    catEditorReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    catEditing = cat;
    catStorageError = '';
    catEditorOpen = true;
    await tick();
    coverDialog?.focus();
  }

  async function cancelCatEditing() {
    const editedId = catEditing?.id;
    catEditorOpen = false;
    catEditing = undefined;
    await tick();
    const restored = editedId ? coverDialog?.querySelector<HTMLElement>(`[data-edit-cat="${editedId}"]`) : coverDialog?.querySelector<HTMLElement>('.new-cat:not(:disabled)');
    if (catEditorReturnFocus?.isConnected) catEditorReturnFocus.focus();
    else if (restored) restored.focus();
    else coverDialog?.focus();
  }

  async function saveCat(profile: CustomCatProfile) {
    const cat = sanitizeCustomCat(profile);
    if (!cat) return;
    if (customCats.length >= 24 && !customCats.some((item) => item.id === cat.id)) {
      catStorageError = 'Your studio has room for 24 cats. Edit one of your companions to keep creating.';
      return;
    }
    const exists = customCats.some((item) => item.id === cat.id);
    const next = exists ? customCats.map((item) => item.id === cat.id ? cat : item) : [...customCats, cat];
    try { localStorage.setItem(CATS_STORAGE_KEY, JSON.stringify(next)); }
    catch { catStorageError = 'Your browser could not save this cat. Your design is still here; try again.'; return; }
    customCats = next;
    if (coverItem) coverCharacterDraft = `custom:${cat.id}`;
    catEditing = cat;
    await cancelCatEditing();
  }

  function itemCover(item: LibraryItem) {
    return item.run ? localState.runMeta[item.run.run_id]?.coverTheme : item.course?.coverTheme;
  }

  async function openCoverEditor(item: LibraryItem) {
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    returnFocus = trigger?.closest('details')?.querySelector<HTMLElement>('summary') || trigger;
    dismissMenus();
    coverDraft = itemCover(item) || '';
    coverCollectionDraft = 'all';
    const character = item.run ? localState.runMeta[item.run.run_id]?.coverCharacter : item.course?.coverCharacter;
    coverCharacterDraft = validCharacter(character) ? character : catForVoice(item.run?.voice || item.course?.voiceKey.split('::').pop() || 'Ryan');
    const pose = item.run ? localState.runMeta[item.run.run_id]?.coverPose : item.course?.coverPose;
    coverPoseDraft = validPose(pose) ? pose : coverPoseForMood(item.run ? runCoverMood(item.run) : item.course?.mood || '');
    catEditorOpen = false;
    coverItem = item;
    await tick();
    coverDialog?.focus();
  }

  function saveCover() {
    if (!coverItem) return;
    const coverTheme = isCoverTheme(coverDraft) ? coverDraft : undefined;
    const coverCharacter = validCharacter(coverCharacterDraft) ? coverCharacterDraft : 'whoopy';
    const coverPose = coverPoseDraft;
    if (coverItem.run) rememberRunPresentation(coverItem.run.run_id, { coverTheme, coverCharacter, coverPose });
    if (coverItem.course) persistLocalState({ ...localState, courses: localState.courses.map((course) => course.id === coverItem?.course?.id ? { ...course, coverTheme, coverCharacter, coverPose } : course) });
    closeDialogs();
  }

  onMount(() => {
    if (new URLSearchParams(window.location.search).get('tab') === 'settings') view = 'settings';
    try { customCats = sanitizeCustomCats(JSON.parse(localStorage.getItem(CATS_STORAGE_KEY) || '[]')); } catch { customCats = []; }
    loadLocalState();
    Promise.allSettled([loadStatus(), loadLibrary()]);
    return () => clearTimeout(pollHandle);
  });
</script>

<svelte:head><meta name="color-scheme" content="light dark" /><title>{view === 'settings' ? 'Settings · Whoopy' : 'Whoopy Studio'}</title></svelte:head>
<svelte:window onkeydown={handleKeydown} onpointerdown={dismissMenus} onfocusin={dismissMenus} onscroll={updateScroll} onresize={() => dismissMenus()} />

<a class="skip-link" href="#main" inert={modalOpen}>Skip to content</a>
<header class="topbar" class:scrolled inert={modalOpen}>
  <div class="topbar-inner">
  <Brand asset="/cat-logo-v5-head-64.png" />
  <nav aria-label="Main navigation">
    <button class:active={view === 'home'} onclick={() => navigate('home')}>Home</button>
    <button class:active={view === 'create'} onclick={() => { if (creating) createMode = 'meditation'; navigate('create'); }}>Create</button>
    <button class:active={view === 'voices'} onclick={() => navigate('voices')}>Voices</button>
    <button class:active={view === 'library'} onclick={() => navigate('library')}>Library</button>
    <button class:active={view === 'settings'} onclick={() => navigate('settings')}>Settings</button>
  </nav>
  <div class="header-tools"><StudioAppearance /><div class="engine" aria-live="polite"><i class:offline={statusLoaded && !engineOnline}></i>{#if creating}<button class="engine-busy" onclick={() => { createMode = 'meditation'; navigate('create'); }} aria-label="View meditation generation progress">Creating…</button>{:else}{!statusLoaded ? 'Connecting…' : engineOnline ? 'Local' : 'Offline'}{/if}</div></div>
  </div>
</header>

<main id="main" class:has-player={Boolean(currentRun)} inert={modalOpen}>
  <div use:viewTransition={view}>
  {#if view === 'home'}
    <section class="home-hero">
      <div class="home-copy">
        <h1>A moment to <span>yourself.</span></h1>
        <p>Create a meditation, or pick up where you left off.</p>
        <div class="home-actions"><button class="primary" onclick={() => { createMode = 'meditation'; navigate('create'); }}>Create meditation</button><button class="secondary" onclick={() => { createMode = 'course'; navigate('create'); }}>Plan a course</button></div>
      </div>
      <div class="studio-room" aria-label="Whoopy resting in her favorite room">
        <div class="home-art-group">
          <div class="home-art"><MeditationCover seed="whoopy-home" character="whoopy" pose="resting" theme="reading" composition="hero" /></div>
        </div>
      </div>
    </section>

    <section id="home-collection" class="studio-route" use:reveal aria-label="Start creating">
      <header><h2>What would help today?</h2></header>
      <div class="practice-paths">
        <button class="play-card calm-card" onclick={() => startIdea(startingIdeas[0])}><span class="play-card-copy"><strong>Meditation</strong><small>A guided practice for how you feel today.</small><b>Make a meditation <span aria-hidden="true">↗</span></b></span><span class="play-card-art"><PracticeIllustration kind="calm" /></span></button>
        <button class="play-card journey-card" onclick={() => { createMode = 'course'; navigate('create'); }}><span class="play-card-copy"><strong>Course</strong><small>Build a practice across a series of sessions.</small><b>Build a course <span aria-hidden="true">↗</span></b></span><span class="play-card-art"><PracticeIllustration kind="journey" /></span></button>
        <button class="play-card friends-card" onclick={() => navigate('voices')}><span class="play-card-copy"><strong>Voices</strong><small>Listen to the guides and choose your favorite.</small><b>Meet the voices <span aria-hidden="true">↗</span></b></span><span class="play-card-art"><PracticeIllustration kind="voices" /></span></button>
      </div>
    </section>

    <section class="home-grid" use:reveal aria-label="Your recent work">
      <div class="continue-panel">
        <header><div><span class="quiet-label">Listen again</span><h2>Recent meditations</h2></div><button class="text-button" onclick={() => navigate('library')}>Open library</button></header>
        {#if libraryError}<p class="inline-notice">Could not load your library: {libraryError}</p>{/if}
        {#if runs.filter((run) => !localState.runMeta[run.run_id]?.trashedAt).length}
          <div class="home-recent">
            {#each runs.filter((run) => !localState.runMeta[run.run_id]?.trashedAt).slice(0, 3) as run}
              <article><button class="recent-art" aria-label={`Open ${cleanTitle(run)}`} onclick={() => openRun(run)}><MeditationCover seed={run.run_id} mood={runCoverMood(run)} theme={localState.runMeta[run.run_id]?.coverTheme} character={localState.runMeta[run.run_id]?.coverCharacter || catForVoice(run.voice || 'Ryan')} pose={localState.runMeta[run.run_id]?.coverPose} customCat={findCustomCat(localState.runMeta[run.run_id]?.coverCharacter, customCats)} compact /></button><button class="title-button" onclick={() => openRun(run)}><strong>{cleanTitle(run)}</strong><small>{duration(run)} · {dateLabel(run.created_at)}</small></button><button class="round-play" disabled={!run.has_audio || run.status !== 'completed'} aria-label={`Play ${cleanTitle(run)}`} onclick={() => playRun(run)}>▶</button></article>
            {/each}
          </div>
        {:else}<div class="compact-empty"><p>Your first meditation will appear here.</p></div>{/if}
      </div>
      <aside class="nook-scene">
        <div class="nook-art" aria-hidden="true"><MeditationCover seed="course-nook" theme="fireplace" pose="sleeping" kind="course" /></div>
        <div class="nook-copy"><span class="quiet-label">Courses</span><h3>Keep a practice going.</h3><p>{localState.courses.filter((course) => !course.trashedAt).length ? `${localState.courses.filter((course) => !course.trashedAt).length} course ${localState.courses.filter((course) => !course.trashedAt).length === 1 ? 'draft' : 'drafts'} to return to.` : 'Create a series of sessions around a goal you care about.'}</p><button class="nook-action" onclick={() => { if (localState.courses.some((course) => !course.trashedAt)) { selectedFolder = 'courses'; libraryKind = 'course'; navigate('library'); } else { createMode = 'course'; navigate('create'); } }}>{localState.courses.some((course) => !course.trashedAt) ? 'Continue a course' : 'Plan a course'} →</button></div>
      </aside>
    </section>

    <section class="home-discovery" use:reveal aria-label="Ways to begin">
      <div class="starting-panel">
        <header><span class="quiet-label">Start with a feeling</span><h2>Try a starting point</h2></header>
        <div class="idea-list illustrated-ideas">{#each startingIdeas as idea, index}<button onclick={() => startIdea(idea)}><span class="idea-art"><PracticeIllustration kind={index === 1 ? 'sleep' : index === 2 ? 'focus' : index === 3 ? 'kindness' : 'calm'} /></span><span class="idea-copy"><strong>{idea.label}</strong><b>Begin <span aria-hidden="true">↗</span></b></span></button>{/each}</div>
      </div>
      <button class="voice-panel" onclick={() => navigate('voices')}>
        <span class="voice-invite-art"><PracticeIllustration kind="voices" /></span>
        <span><small>Meet the voices</small><strong>Find a voice you enjoy listening to.</strong><b>Meet Whoopy and friends →</b></span>
      </button>
    </section>
  {:else if view === 'voices'}
    <section class="voices-page" use:reveal>
      <header class="voices-page-head"><div><span class="quiet-label">The voice room</span><h1>Who’s keeping<br />you company?</h1><p>Meet the cats. Find a voice that feels familiar.</p></div><div class="voice-header-art" aria-hidden="true"><MeditationCover seed="voices-whoopy" theme="reading" pose="resting" /></div></header>
      <VoiceWindow options={availableVoices} selectedKey={selectedVoiceKey} {previewingKey} onChoose={chooseVoice} onPreview={previewVoice} mode="page" />
      <div class="cat-studio-invite"><div><span class="quiet-label">A companion of your own</span><h2>There’s room for your cat, too.</h2><p>Choose a coat, markings, and eyes. Create a companion for your meditation covers.</p></div><button class="secondary" onclick={openCatStudio}>Open cat studio ↗</button></div>
      <footer class="voice-page-footer"><span>Selected: <strong>{activeVoice?.label || 'No voice selected'}</strong></span><button class="primary compact" onclick={() => navigate('create')}>Create with this voice →</button></footer>
    </section>
  {:else if view === 'create'}
    <section class="create-shell">
      <header class="create-intro">
        <div class="create-heading"><span class="quiet-label">Creation studio</span><h1>Shape a practice.</h1><p>Start simple. Fine-tune only what matters.</p></div>
        <div class="create-illustration"><PracticeIllustration kind={createMode === 'course' ? 'journey' : 'calm'} /></div>
        <CreatorModeSwitch bind:mode={createMode} disabled={creating} />
      </header>
      <div class="create-workspace">
        <section class="generator-card">
          {#if createMode === 'meditation'}
            {#if creating}
              <div class="working"><GenerationProgress run={generationRun} {connectionLost} />{#if error}<p class="error" role="alert">{error}</p>{/if}<div class="working-actions"><button class="text-button" onclick={continueInLibrary}>Continue in the library</button>{#if connectionLost}<button class="secondary compact" onclick={retryGenerationConnection}>Retry connection</button>{:else}<button class="secondary compact" onclick={cancelGeneration} disabled={!generatingRunId || cancelling}>{cancelling ? 'Stopping…' : 'Cancel'}</button>{/if}</div></div>
            {:else}
              <label class="prompt-label" for="prompt">What do you need right now?</label>
              <textarea id="prompt" bind:value={input} maxlength="20000" placeholder="I want to settle after a busy day."></textarea>
              <fieldset><legend>How should it feel?</legend><div class="mood-row">{#each moods as item, index}<button class:selected={mood === item.id} onclick={() => mood = item.id} aria-pressed={mood === item.id}><span class="mood-symbol"><PracticeIllustration kind={index === 1 ? 'sleep' : index === 2 ? 'focus' : index === 3 ? 'kindness' : 'calm'} /></span>{item.label}</button>{/each}</div></fieldset>
              <div class="choice-row">
                <fieldset class="duration-choice"><legend>Length</legend><div>{#each [3, 5, 10, 15, 20] as option}<button class:selected={minutes === option} aria-pressed={minutes === option} onclick={() => minutes = option}>{option}<small>min</small></button>{/each}</div></fieldset>
                <div class="voice-summary"><span>Voice</span><button onclick={openVoiceChooser}><strong>{activeVoice?.label || 'Choose voice'}</strong><small>{activeVoice?.description || 'Local voice'} · Choose</small></button></div>
              </div>
              <button class="fine-tune-toggle" onclick={() => showAdvanced = !showAdvanced} aria-expanded={showAdvanced}><span><strong>Voice & writing options</strong><small>{pace === 'slow' ? 'Unhurried pace' : 'Natural pace'} · {writingProfiles.length === 1 ? 'Standard writer' : writingProfile === 'high' ? 'Detailed writer' : 'Lightweight writer'}</small></span><b>{showAdvanced ? '−' : '+'}</b></button>
              {#if showAdvanced}<section class="fine-tune-panel" aria-label="Meditation settings"><div class="settings-grid"><label>Writing model<select bind:value={writingProfile}>{#each writingProfiles as profile}<option value={profile}>{profile === 'high' ? 'Ministral 3 14B · detailed' : writingProfiles.length === 1 ? 'Standard local writer' : 'Qwen3 4B · lightweight'}</option>{/each}</select></label><label>Delivery<select bind:value={delivery}>{#each deliveryOptions(activeVoice?.modelId) as option}<option value={option}>{option}</option>{/each}</select></label><label>Pace<select bind:value={pace}><option value="slow">Slow</option><option value="natural">Natural</option></select></label></div>{#if writingProfiles.length > 1}<p class="settings-note">A larger writer can take more memory and time on this device.</p>{/if}</section>{/if}
              {#if error}<p class="error" role="alert">{error}</p>{/if}
              {#if statusLoaded && !engineOnline}<p class="error" role="status">Start the local engine to create. {engineError}</p>{/if}
              <button class="primary wide" onclick={generate} disabled={!engineOnline || !voiceCanGenerate(activeVoice)}>Create meditation</button>
            {/if}
          {:else}
            {#if courseStep === 'setup'}
              <div class="course-heading"><span class="draft-badge">Local draft</span><h2>Plan a course</h2><p>Choose a focus and a little time. We’ll sketch an editable session outline, saved on this device.</p></div>
              <fieldset class="course-presets"><legend>Choose a starting path</legend><div>{#each coursePresets as preset}<button class:selected={coursePreset === preset.id} aria-pressed={coursePreset === preset.id} onclick={() => chooseCoursePreset(preset.id)}>{preset.label}</button>{/each}</div></fieldset>
              <label class="prompt-label" for="course-goal">What should the course help with?</label><textarea id="course-goal" bind:value={courseGoal} placeholder="Build a calmer relationship with work."></textarea>
              <div class="choice-row course-basics"><label>How many sessions?<select bind:value={courseCount}>{#each COURSE_COUNTS as count}<option value={count}>{count} sessions</option>{/each}</select></label><label>A little time each day<select bind:value={courseMinutes}><option value={3}>3 minutes</option><option value={5}>5 minutes</option><option value={10}>10 minutes</option><option value={15}>15 minutes</option></select></label></div>
              <button class="fine-tune-toggle" onclick={() => showCourseAdvanced = !showCourseAdvanced} aria-expanded={showCourseAdvanced}><span><strong>Personalize your path</strong><small>{moodLabel(courseMood)} · {activeVoice?.label || 'Whoopy'}</small></span><b>{showCourseAdvanced ? '−' : '+'}</b></button>
              {#if showCourseAdvanced}<section class="fine-tune-panel course-settings" aria-label="Journey settings"><label>Feeling<select bind:value={courseMood}>{#each moods as item}<option value={item.id}>{item.label}</option>{/each}</select></label><div class="voice-summary standalone"><span>Voice for your course</span><button onclick={openVoiceChooser}><strong>{activeVoice?.label || 'Choose voice'}</strong><small>{activeVoice?.description || 'Local voice'} · Choose</small></button></div><p class="settings-note">These preferences are saved with your outline. Course audio generation is coming later.</p></section>{/if}
              <button class="primary wide" onclick={makeCoursePlan} disabled={!courseGoal.trim()}>Sketch my course →</button>
            {:else}
              <div class="course-review-head"><div><span class="draft-badge">Draft v{courseRevision + 1}</span><input class="course-title" aria-label="Course title" bind:value={courseTitle} /></div><button class="secondary compact" onclick={() => courseStep = 'setup'}>Back to setup</button></div>
              <p class="review-note">Your outline is ready to shape. Edit a title, move a session, or add your own. This saves a plan; audio generation comes later.</p>
              <div class="course-plan-summary"><strong>{courseSessions.length} sessions</strong><span>{courseMinutes} min each</span><span>{moodLabel(courseMood)}</span><span>{activeVoice?.label || 'Whoopy'}</span></div>
              <CourseOutline sessions={courseSessions} onMove={moveSession} onRemove={removeSession} />
              <button class="add-session" onclick={addSession}>+ Add session</button>
              <div class="review-actions"><button class="text-button" onclick={resetCourse}>Start over</button><button class="primary" disabled={!courseSessions.length || !courseTitle.trim()} onclick={saveCourseDraft}>Save course outline</button></div>
            {/if}
          {/if}
        </section>
        <aside class="creation-art" aria-label="Whoopy keeping you company"><div class="creation-art-image"><MeditationCover seed="creation-room" kind={createMode} theme={createMode === 'course' ? 'sunroom' : 'night'} pose={createMode === 'course' ? 'seated' : 'sleeping'} /></div><p>{createMode === 'course' ? 'A small practice. A familiar place.' : 'No need to rush.'}</p></aside>
      </div>
    </section>
  {:else if view === 'settings'}
    <StudioSettings />
  {:else}
    <section class="library-page">
      <header class="library-head">
        <div class="library-copy"><span class="quiet-label">Made here. Kept here.</span><h1>Your library.</h1><p>A shelf for every practice you want to come back to.</p><div class="library-head-actions"><button class="primary compact" onclick={() => { createMode = 'meditation'; navigate('create'); }}>＋ New meditation</button><button class="secondary compact" onclick={() => openFolderDialog()}>New folder</button><button class="secondary compact" onclick={openCatStudio}>Cat studio</button></div></div>
        <div class="library-vignette" aria-hidden="true"><MeditationCover seed="library-nook" theme="reading" pose="sleeping" /></div>
      </header>
      {#if libraryError}<div class="error library-error" role="alert">{libraryError}<button onclick={loadLibrary}>Try again</button></div>{/if}
      <div class="library-shell">
        <aside class="library-nav" aria-label="Library sections">
          <button class:active={selectedFolder === 'all'} onclick={() => { selectedFolder = 'all'; libraryKind = 'all'; }}><span>All</span><small>{runs.filter((run) => !localState.runMeta[run.run_id]?.trashedAt).length + localState.courses.filter((course) => !course.trashedAt).length}</small></button>
          <button class:active={selectedFolder === 'meditations'} onclick={() => { selectedFolder = 'meditations'; libraryKind = 'meditation'; }}><span>Meditations</span><small>{runs.filter((run) => !localState.runMeta[run.run_id]?.trashedAt).length}</small></button>
          <button class:active={selectedFolder === 'courses'} onclick={() => { selectedFolder = 'courses'; libraryKind = 'course'; }}><span>Courses</span><small>{localState.courses.filter((course) => !course.trashedAt).length}</small></button>
          <div class="nav-label">Folders</div>
          {#each localState.folders.filter((folder) => !folder.trashedAt) as folder}<div class="folder-link"><button class:active={selectedFolder === folder.id} onclick={() => { selectedFolder = folder.id; libraryKind = 'all'; }}><span>{folder.name}</span><small>{folderCount(folder.id)}</small></button><details data-action-menu ontoggle={positionActionMenu}><summary aria-label={`Folder actions for ${folder.name}`}>•••</summary><div><button onclick={() => renameFolder(folder)}>Rename</button><button onclick={() => trashFolder(folder)}>Move to Trash</button></div></details></div>{/each}
          <button class:active={selectedFolder === 'trash'} onclick={() => { selectedFolder = 'trash'; libraryKind = 'all'; }}><span>Trash</span><small>{Object.values(localState.runMeta).filter((meta) => meta.trashedAt).length + localState.courses.filter((course) => course.trashedAt).length + localState.folders.filter((folder) => folder.trashedAt).length}</small></button>
        </aside>
        <section class="library-content">
          <header class="shelf-heading"><div><span class="quiet-label">On your shelf</span><h2>{selectedFolder === 'all' ? 'Everything you’ve made' : selectedFolder === 'meditations' ? 'Your meditations' : selectedFolder === 'courses' ? 'Your courses' : selectedFolder === 'trash' ? 'Recently set aside' : localState.folders.find((folder) => folder.id === selectedFolder)?.name || 'Your collection'}</h2></div><div class="layout-switch" aria-label="Library layout"><button class:active={libraryLayout === 'grid'} aria-label="Grid view" aria-pressed={libraryLayout === 'grid'} onclick={() => libraryLayout = 'grid'}><svg viewBox="0 0 20 20" aria-hidden="true"><rect x="3" y="3" width="5" height="5" rx="1"/><rect x="12" y="3" width="5" height="5" rx="1"/><rect x="3" y="12" width="5" height="5" rx="1"/><rect x="12" y="12" width="5" height="5" rx="1"/></svg></button><button class:active={libraryLayout === 'list'} aria-label="List view" aria-pressed={libraryLayout === 'list'} onclick={() => libraryLayout = 'list'}><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 5h12M4 10h12M4 15h12"/></svg></button></div></header>
          <div class="shelf-tabs" aria-label="Quick status filters">{#each [{id: 'all', label: 'All practices'}, {id: 'ready', label: 'Ready to listen'}, {id: 'creating', label: 'In the making'}, {id: 'attention', label: 'Needs attention'}, {id: 'draft', label: 'Drafts'}] as filter}<button class:active={libraryState === filter.id} aria-pressed={libraryState === filter.id} onclick={() => libraryState = filter.id as LibraryState}>{filter.label}</button>{/each}</div>
          <div class="library-tools"><label class="search-field"><span>Search</span><input bind:value={libraryQuery} placeholder="Title, voice, or course" /></label><label>Type<select bind:value={libraryKind}><option value="all">Everything</option><option value="meditation">Meditations</option><option value="course">Courses</option></select></label><label class="status-filter">Status<select bind:value={libraryState}><option value="all">Any status</option><option value="ready">Ready</option><option value="creating">Creating</option><option value="attention">Needs attention</option><option value="draft">Draft</option></select></label><label>Sort<select bind:value={librarySort}><option value="newest">Newest</option><option value="oldest">Oldest</option><option value="title">Title</option><option value="duration">Duration</option></select></label></div>
          <div class="results-caption" aria-live="polite"><span>{libraryResults.length} {libraryResults.length === 1 ? 'item' : 'items'}{libraryQuery ? ` matching “${libraryQuery}”` : ''}</span>{#if libraryQuery || libraryState !== 'all' || libraryKind !== 'all'}<button class="text-button" onclick={() => { libraryQuery = ''; libraryState = 'all'; libraryKind = 'all'; }}>Clear filters</button>{/if}</div>
          <div class="library-results" class:card-layout={libraryLayout === 'grid'} class:row-layout={libraryLayout === 'list'}>
            {#each libraryResults.slice(0, libraryLimit) as item (item.key)}
              <article class:course-row={item.kind === 'course'} class:folder-row={item.kind === 'folder'}>
                <div class="shelf-cover"><button class="cover-open" aria-label={`Open ${item.title}`} onclick={() => item.run ? openRun(item.run) : item.course ? editCourse(item.course) : undefined} disabled={item.kind === 'folder'}><MeditationCover kind={item.kind === 'course' ? 'course' : 'meditation'} seed={item.run?.run_id || item.key} mood={item.run ? runCoverMood(item.run) : item.course?.mood} theme={item.run ? localState.runMeta[item.run.run_id]?.coverTheme : item.course?.coverTheme} character={(item.run ? localState.runMeta[item.run.run_id]?.coverCharacter : item.course?.coverCharacter) || catForVoice(item.run?.voice || item.course?.voiceKey.split('::').pop() || 'Ryan')} pose={item.run ? localState.runMeta[item.run.run_id]?.coverPose : item.course?.coverPose} customCat={findCustomCat(item.run ? localState.runMeta[item.run.run_id]?.coverCharacter : item.course?.coverCharacter, customCats)} /></button><span class="cover-kind">{item.kind === 'course' ? `${item.course?.sessions.length} sessions` : item.kind === 'folder' ? 'Folder' : item.durationSeconds ? `${Math.max(1, Math.round(item.durationSeconds / 60))} min` : 'Meditation'}</span>{#if item.run}<button class="cover-play" disabled={!item.run.has_audio || item.run.status !== 'completed'} aria-label={`Play ${item.title}`} onclick={() => playRun(item.run!)}>▶</button>{/if}</div>
                <button class="title-button" onclick={() => item.run ? openRun(item.run) : item.course ? editCourse(item.course) : undefined}><strong>{item.title}</strong>{#if item.tag}<span class="purpose-tag">{item.tag}</span>{/if}<small>{item.run ? `${libraryLayout === 'list' ? `${duration(item.run)} · ` : ''}${voiceCharacterLabel(item.run.voice || 'Local voice')} · ${dateLabel(item.createdAt)}` : item.subtitle}</small></button>
                <span class="state {item.state}">{item.state === 'attention' ? 'needs attention' : item.state}</span>
                {#if selectedFolder === 'trash'}<button class="restore" onclick={() => restoreItem(item)}>Restore</button>{:else}<details class="item-actions" data-action-menu ontoggle={positionActionMenu}><summary aria-label={`Actions for ${item.title}`}>•••</summary><div>{#if item.course}<button onclick={() => editCourse(item.course!)}>Edit course</button>{/if}{#if item.run}<button onclick={() => renameRun(item.run!)}>Rename</button><button onclick={() => duplicateRunSettings(item.run!)}>Duplicate settings</button>{#if localState.courses.filter((course) => !course.trashedAt).length}<label>Add to course<select value="" onchange={(event) => { addRunToCourse(item.run!, event.currentTarget.value); event.currentTarget.value = ''; }}><option value="">Choose course</option>{#each localState.courses.filter((course) => !course.trashedAt) as course}<option value={course.id}>{course.title}</option>{/each}</select></label>{/if}{#if item.run.has_audio}<a href={`/api/v1/runs/${item.run.run_id}/audio`} download>Download audio</a>{/if}{/if}{#if item.kind !== 'folder'}<button onclick={() => openCoverEditor(item)}>Change cover</button><label>Move to<select value={item.folderId || ''} onchange={(event) => item.run ? moveRun(item.run.run_id, event.currentTarget.value) : item.course ? moveCourse(item.course.id, event.currentTarget.value) : undefined}><option value="">No folder</option>{#each localState.folders.filter((folder) => !folder.trashedAt) as folder}<option value={folder.id}>{folder.name}</option>{/each}</select></label><button class="danger-text" onclick={() => trashItem(item)}>Move to Trash</button>{/if}</div></details>{/if}
              </article>
            {:else}
              <div class="library-empty"><div class="empty-art" aria-hidden="true"><MeditationCover seed="empty-library" theme="night" pose="sleeping" /></div><h2>{libraryQuery || libraryState !== 'all' || libraryKind !== 'all' ? 'No matches this time.' : selectedFolder === 'trash' ? 'Trash is empty.' : 'Nothing here yet.'}</h2><p>{libraryQuery || libraryState !== 'all' || libraryKind !== 'all' ? 'A different search or filter may bring it into view.' : 'There’s room here for your next quiet moment.'}</p>{#if libraryQuery || libraryState !== 'all' || libraryKind !== 'all'}<button class="secondary compact" onclick={() => { libraryQuery = ''; libraryState = 'all'; libraryKind = 'all'; }}>Clear filters</button>{:else if selectedFolder !== 'trash'}<button class="primary compact" onclick={() => navigate('create')}>Create a meditation</button>{/if}</div>
            {/each}
          </div>
          {#if libraryResults.length > libraryLimit}<div class="load-more"><p>Showing {libraryLimit} of {libraryResults.length}</p><button class="secondary compact" onclick={() => libraryLimit += 12}>Show 12 more</button></div>{/if}
        </section>
      </div>
    </section>
  {/if}
  </div>
  <footer class="studio-footer"><span>Whoopy Studio</span><span>Local meditation</span></footer>
</main>

{#if folderEditing}
  <FolderDialog initialName={folderEditing === 'new' ? '' : folderEditing.name} editing={folderEditing !== 'new'} onSave={saveFolder} onClose={closeDialogs} />
{/if}

<audio bind:this={previewAudio} onended={() => previewingKey = ''} aria-hidden="true"></audio>

{#if showVoiceChooser}
  <div class="modal-backdrop" role="presentation" onclick={(event) => event.currentTarget === event.target && closeDialogs()}>
    <div class="voice-drawer" bind:this={voiceDialog} tabindex="-1" role="dialog" aria-modal="true" aria-labelledby="voice-title">
      <header><div><span class="quiet-label">The voice den</span><h2 id="voice-title">Who should guide you?</h2><p>Meet each voice before choosing.</p></div><button class="close" aria-label="Close voice chooser" onclick={closeDialogs}>×</button></header>
      <VoiceWindow options={availableVoices} selectedKey={selectedVoiceKey} {previewingKey} onChoose={chooseVoice} onPreview={previewVoice} mode="dialog" />
    </div>
  </div>
{/if}

{#if coverItem || showCatStudio}
  <div class="modal-backdrop" role="presentation" onclick={(event) => event.currentTarget === event.target && closeDialogs()}>
    <div class="cover-editor personalisation-dialog" class:editing-cat={catEditorOpen} bind:this={coverDialog} tabindex="-1" role="dialog" aria-modal="true" aria-label={catEditorOpen ? 'Cat creator' : coverItem ? (coverItem.kind === 'course' ? 'Customize course cover' : 'Customize meditation cover') : 'Your cat studio'}>
      {#if catStorageError}<p class="error" role="alert">{catStorageError}</p>{/if}
      {#if catEditorOpen}
        <CatCreator initial={catEditing} onSave={saveCat} onCancel={cancelCatEditing} />
      {:else}
        <header><div><span class="quiet-label">Make it yours</span><h2>{coverItem ? (coverItem.kind === 'course' ? 'Customize course cover' : 'Customize meditation cover') : 'Cat studio'}</h2><p>{coverItem ? coverItem.title : 'Choose a companion or create your own.'}</p></div><button class="close" aria-label={coverItem ? 'Close cover editor' : 'Close cat studio'} onclick={closeDialogs}>×</button></header>
        {#if coverItem}<div class="cover-editor-preview"><MeditationCover kind={coverItem.kind === 'course' ? 'course' : 'meditation'} seed={coverItem.run?.run_id || coverItem.key} mood={coverItem.run ? runCoverMood(coverItem.run) : coverItem.course?.mood} theme={coverDraft} character={coverCharacterDraft} pose={coverPoseDraft} customCat={findCustomCat(coverCharacterDraft, customCats)} /></div>{/if}
        <fieldset class="cat-choices"><legend>{coverItem ? '1. Choose your companion' : 'The original companions'}</legend><div>{#each catCharacters as character}<button class:selected={Boolean(coverItem) && coverCharacterDraft === character} aria-pressed={Boolean(coverItem) && coverCharacterDraft === character} aria-label={coverItem ? catNames[character] : `Start with ${catNames[character]}`} onclick={() => coverItem ? coverCharacterDraft = character : startPresetCat(character)}><img src={catPortraits[character]} alt="" /><span>{catNames[character]}</span>{#if character === 'whoopy'}<small>Your host</small>{/if}</button>{/each}</div></fieldset>
        <section class="your-cats" aria-label="Your cats"><header><div><h3>Your companions</h3><p>{customCats.length ? 'Made by you. Saved here.' : 'Create a companion for your covers.'}</p></div><button class="secondary compact new-cat" disabled={customCats.length >= 24} onclick={() => editCat()}>＋ Create a cat</button></header>
          {#if customCats.length}<div class="custom-cat-list">{#each customCats as cat (cat.id)}<article class:selected={Boolean(coverItem) && coverCharacterDraft === `custom:${cat.id}`}><button class="custom-cat-select" aria-label={coverItem ? `Choose ${cat.name}` : cat.name} aria-pressed={Boolean(coverItem) && coverCharacterDraft === `custom:${cat.id}`} disabled={!coverItem} onclick={() => coverCharacterDraft = `custom:${cat.id}`}><span><CustomCat profile={cat} /></span><strong>{cat.name}</strong></button><button class="edit-cat" data-edit-cat={cat.id} aria-label={`Edit ${cat.name}`} onclick={() => editCat(cat)}>Edit</button></article>{/each}</div>{/if}
        </section>
        {#if coverItem}
          <fieldset class="pose-choices"><legend>2. Pick a pose</legend><div>{#each catPoses as pose}<button class:selected={coverPoseDraft === pose.id} aria-pressed={coverPoseDraft === pose.id} onclick={() => coverPoseDraft = pose.id}><span class="pose-art"><MeditationCover kind={coverItem.kind === 'course' ? 'course' : 'meditation'} seed={coverItem.run?.run_id || coverItem.key} mood={coverItem.run ? runCoverMood(coverItem.run) : coverItem.course?.mood} theme={coverDraft || undefined} character={coverCharacterDraft} pose={pose.id} customCat={findCustomCat(coverCharacterDraft, customCats)} /></span>{pose.label}</button>{/each}</div></fieldset>
          <fieldset class="cover-choices"><legend>3. Choose a setting</legend><CoverCollectionFilter bind:value={coverCollectionDraft} kind={coverItem.kind === 'course' ? 'course' : 'meditation'} /><div>{#each visibleCoverThemes as theme}<button class:selected={coverDraft === theme} aria-pressed={coverDraft === theme} onclick={() => coverDraft = theme}><span class="cover-choice-art"><MeditationCover kind={coverItem.kind === 'course' ? 'course' : 'meditation'} seed={coverItem!.key} {theme} character={coverCharacterDraft} pose={coverPoseDraft} customCat={findCustomCat(coverCharacterDraft, customCats)} /></span><span>{coverThemeLabels[theme]}</span></button>{/each}</div></fieldset>
          <button class="cover-auto" class:selected={!coverDraft} aria-pressed={!coverDraft} onclick={() => coverDraft = ''}>Choose scene automatically <span>{!coverDraft ? '✓' : '↺'}</span></button>
          <footer><span class="cover-footer-hint"><span>Saved on this device.</span><button class="preview-jump" onclick={() => coverDialog?.scrollTo({ top: 0, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' })}>See preview <span aria-hidden="true">↑</span></button></span><button class="primary compact" onclick={saveCover}>Save cover</button></footer>
        {:else}<footer><span>Your cats appear on covers. Choose a narrator on the Voices page.</span><button class="primary compact" onclick={closeDialogs}>Done</button></footer>{/if}
      {/if}
    </div>
  </div>
{/if}

{#if selectedRun}
  <div class="modal-backdrop" role="presentation" onclick={(event) => event.currentTarget === event.target && closeDialogs()}>
    <div class="run-detail" bind:this={runDialog} tabindex="-1" role="dialog" aria-modal="true" aria-labelledby="player-title">
      <button class="close" aria-label="Close details" onclick={closeDialogs}>×</button>
      <div class="detail-stage detail-cover"><MeditationCover seed={selectedRun.run_id} mood={runCoverMood(selectedRun)} theme={localState.runMeta[selectedRun.run_id]?.coverTheme} character={localState.runMeta[selectedRun.run_id]?.coverCharacter || catForVoice(selectedRun.voice || 'Ryan')} pose={localState.runMeta[selectedRun.run_id]?.coverPose} customCat={findCustomCat(localState.runMeta[selectedRun.run_id]?.coverCharacter, customCats)} /></div>
      <span class="quiet-label">Meditation</span><h2 id="player-title">{cleanTitle(selectedRun)}</h2>
      <div class="detail-meta"><span>{duration(selectedRun)}</span><span>{voiceCharacterLabel(selectedRun.voice || 'Local voice')}</span><span>{fullDate(selectedRun.created_at)}</span></div>
      {#if selectedRun.has_audio && selectedRun.status === 'completed'}<audio class="detail-audio" controls preload="metadata" src={`/api/v1/runs/${selectedRun.run_id}/audio`} aria-label={`Playback for ${cleanTitle(selectedRun)}`}></audio><div class="detail-actions"><button class="secondary compact" aria-label="Previous course session" disabled={!adjacentCourseRun(selectedRun, -1)} onclick={() => moveDetailSession(-1)}>‹ Previous</button><button class="primary compact" onclick={() => { playRun(selectedRun!); closeDialogs(); }}>Keep playing</button><button class="secondary compact" aria-label="Next course session" disabled={!adjacentCourseRun(selectedRun, 1)} onclick={() => moveDetailSession(1)}>Next ›</button></div><a href={`/api/v1/runs/${selectedRun.run_id}/audio`} download>Download audio</a>{:else}<div class="not-ready {runState(selectedRun)}">{runState(selectedRun) === 'attention' ? 'This meditation needs attention.' : 'This meditation is still being created.'}</div>{/if}
    </div>
  </div>
{/if}

{#if currentRun}
  <aside class="mini-player" aria-label="Now playing" inert={modalOpen}><div class="mini-cover" aria-hidden="true"><MeditationCover seed={currentRun.run_id} mood={runCoverMood(currentRun)} theme={localState.runMeta[currentRun.run_id]?.coverTheme} character={localState.runMeta[currentRun.run_id]?.coverCharacter || catForVoice(currentRun.voice || 'Ryan')} pose={localState.runMeta[currentRun.run_id]?.coverPose} customCat={findCustomCat(localState.runMeta[currentRun.run_id]?.coverCharacter, customCats)} compact /></div><button class="mini-title" onclick={() => openRun(currentRun!)}><small>{voiceCharacterLabel(currentRun.voice || 'Local voice')} · {duration(currentRun)}</small><strong>{cleanTitle(currentRun)}</strong></button><span class="playing-pill">{audioPlaying ? 'Playing' : 'Paused'}</span><div class="course-skip"><button aria-label="Previous course session" disabled={!adjacentCourseRun(currentRun, -1)} onclick={() => playRun(adjacentCourseRun(currentRun!, -1)!)}>‹</button><button aria-label="Next course session" disabled={!adjacentCourseRun(currentRun, 1)} onclick={() => playRun(adjacentCourseRun(currentRun!, 1)!)}>›</button></div><audio bind:this={miniAudio} onplay={() => audioPlaying = true} onpause={() => audioPlaying = false} onended={() => audioPlaying = false} controls preload="metadata" src={`/api/v1/runs/${currentRun.run_id}/audio`}></audio><button class="mini-close" aria-label="Close player" onclick={() => { miniAudio?.pause(); currentRun = null; }}>×</button></aside>
{/if}

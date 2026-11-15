export type GenerationRun = {
  status?: string;
  execution?: { stage?: string; message?: string | null; started_at?: string | null; current_segment_id?: string | null } | null;
  recovery?: { speech_segments_total?: number; speech_segments_completed?: number } | null;
};
export const generationSteps = ['Plan', 'Write', 'Voice', 'Finish'] as const;
const stages: Record<string, { index: number; title: string; detail: string }> = {
  queued: { index:0, title:'Getting your quiet ready', detail:'Waiting for the local engine to begin.' },
  planning: { index:0, title:'Finding a gentle direction', detail:'Shaping the practice around your intention.' },
  drafting: { index:1, title:'Writing your meditation', detail:'Making room for a beginning, a practice, and a gentle ending.' },
  compiling: { index:1, title:'Giving the words a little space', detail:'Preparing the script and its pauses for narration.' },
  waiting_for_model_slot: { index:2, title:'Waiting for the voice engine', detail:'Your script is ready. The voice engine will begin when it is available.' },
  model_startup: { index:2, title:'Waking up your chosen voice', detail:'Loading the voice model on this device.' },
  synthesizing: { index:2, title:'Bringing your meditation to life', detail:'Creating the narration, one passage at a time.' },
  assembling: { index:3, title:'Putting your practice together', detail:'Joining the narration and its quiet spaces.' },
  encoding: { index:3, title:'Preparing it for listening', detail:'Saving the finished audio.' },
  quality_check: { index:3, title:'Listening for the final details', detail:'Checking the audio before it joins your library.' },
  completed: { index:4, title:'Your quiet is ready', detail:'Your meditation is ready to listen to.' }
};
const aliases: Record<string,string> = { plan:'planning', script:'drafting', timeline:'compiling', voice:'synthesizing', speech:'synthesizing', audio:'synthesizing', assembly:'assembling', quality:'quality_check' };
export function generationProgress(run: GenerationRun | null | undefined) {
  const raw = run?.execution?.stage?.toLowerCase() || 'queued';
  const stage = run?.status === 'completed' ? 'completed' : aliases[raw] || raw;
  const current = stages[stage] || { index:0, title:'Preparing your meditation', detail:'Waiting for the engine’s next update.' };
  const total = run?.recovery?.speech_segments_total;
  const count = run?.recovery?.speech_segments_completed;
  const segments = Number.isInteger(total) && total! > 0 && Number.isFinite(count)
    ? { total:Math.floor(total!), completed:Math.max(0,Math.min(Math.floor(count!),Math.floor(total!))) } : null;
  return { ...current, stage, segments, halted:run?.status === 'failed' || run?.status === 'interrupted' };
}

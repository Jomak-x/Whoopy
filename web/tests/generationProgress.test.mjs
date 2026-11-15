import test from 'node:test';
import assert from 'node:assert/strict';
import { generationProgress } from '../apps/studio/src/lib/generationProgress.ts';
test('durable engine stages advance the four-stage rail',()=>{
 const expected={queued:0,planning:0,drafting:1,compiling:1,model_startup:2,waiting_for_model_slot:2,synthesizing:2,assembling:3,encoding:3,quality_check:3};
 for(const [stage,index] of Object.entries(expected)) assert.equal(generationProgress({status:'running',execution:{stage}}).index,index);
});
test('completion is based on engine state rather than elapsed time',()=>{
 assert.equal(generationProgress({status:'completed',execution:{stage:'quality_check'}}).index,4);
 assert.equal(generationProgress({status:'running',execution:{stage:'synthesizing',started_at:'2000-01-01T00:00:00Z'}}).index,2);
 assert.equal(generationProgress({status:'interrupted',execution:{stage:'synthesizing'}}).halted,true);
});
test('voice counts are measured, bounded, and absent without valid totals',()=>{
 assert.deepEqual(generationProgress({recovery:{speech_segments_total:8,speech_segments_completed:3}}).segments,{total:8,completed:3});
 assert.deepEqual(generationProgress({recovery:{speech_segments_total:8,speech_segments_completed:99}}).segments,{total:8,completed:8});
 for(const total of [undefined,0,-4,Infinity,.5]) assert.equal(generationProgress({recovery:{speech_segments_total:total,speech_segments_completed:3}}).segments,null);
 assert.equal(generationProgress({recovery:{speech_segments_total:8}}).segments,null);
});

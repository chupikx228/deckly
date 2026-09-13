import { JOB_STAGE, type GenerationJob, type JobStage } from '@deckly/api-contract';

import { GENERATION } from '@/shared/config';

export const STAGE_LABEL_KEY = {
  [JOB_STAGE.PLANNING]: 'stage.planning',
  [JOB_STAGE.RETRIEVING_SOURCES]: 'stage.retrieving_sources',
  [JOB_STAGE.PARSING_SOURCES]: 'stage.parsing_sources',
  [JOB_STAGE.GENERATING_CARDS]: 'stage.generating_cards',
  [JOB_STAGE.FETCHING_MEDIA]: 'stage.fetching_media',
  [JOB_STAGE.FINALIZING]: 'stage.finalizing',
} as const satisfies Record<JobStage, string>;

export const getElapsedMs = (job: GenerationJob): number =>
  Date.parse(job.updatedAt) - Date.parse(job.createdAt);

export const isGenerationTimedOut = (job: GenerationJob): boolean =>
  getElapsedMs(job) > GENERATION.TIMEOUT_MS;

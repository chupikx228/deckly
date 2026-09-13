export const NOTE_TYPE = {
  BASIC: 'basic',
  BASIC_REVERSED: 'basic_reversed',
  BASIC_OPTIONAL_REVERSED: 'basic_optional_reversed',
  BASIC_TYPE_IN: 'basic_type_in',
  CLOZE: 'cloze',
  MULTIPLE_CHOICE: 'multiple_choice',
  IMAGE_OCCLUSION: 'image_occlusion',
} as const;

export type NoteType = (typeof NOTE_TYPE)[keyof typeof NOTE_TYPE];

export const DIFFICULTY = {
  BEGINNER: 'beginner',
  INTERMEDIATE: 'intermediate',
  ADVANCED: 'advanced',
} as const;

export type Difficulty = (typeof DIFFICULTY)[keyof typeof DIFFICULTY];

export const JOB_STATUS = {
  QUEUED: 'queued',
  RUNNING: 'running',
  SUCCEEDED: 'succeeded',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
} as const;

export type JobStatus = (typeof JOB_STATUS)[keyof typeof JOB_STATUS];

export const TERMINAL_JOB_STATUSES: readonly JobStatus[] = [
  JOB_STATUS.SUCCEEDED,
  JOB_STATUS.FAILED,
  JOB_STATUS.CANCELLED,
];

export const isTerminalJobStatus = (status: JobStatus | undefined): boolean =>
  status !== undefined && TERMINAL_JOB_STATUSES.includes(status);

export const JOB_STAGE = {
  PLANNING: 'planning',
  RETRIEVING_SOURCES: 'retrieving_sources',
  PARSING_SOURCES: 'parsing_sources',
  GENERATING_CARDS: 'generating_cards',
  FETCHING_MEDIA: 'fetching_media',
  FINALIZING: 'finalizing',
} as const;

export type JobStage = (typeof JOB_STAGE)[keyof typeof JOB_STAGE];

export const JOB_STAGE_ORDER = [
  JOB_STAGE.PLANNING,
  JOB_STAGE.RETRIEVING_SOURCES,
  JOB_STAGE.PARSING_SOURCES,
  JOB_STAGE.GENERATING_CARDS,
  JOB_STAGE.FETCHING_MEDIA,
  JOB_STAGE.FINALIZING,
] as const;

export const ERROR_CODE = {
  VALIDATION_FAILED: 'VALIDATION_FAILED',
  TOPIC_REJECTED: 'TOPIC_REJECTED',
  RATE_LIMITED: 'RATE_LIMITED',
  JOB_NOT_FOUND: 'JOB_NOT_FOUND',
  JOB_ALREADY_TERMINAL: 'JOB_ALREADY_TERMINAL',
  GENERATION_FAILED: 'GENERATION_FAILED',
  UPSTREAM_UNAVAILABLE: 'UPSTREAM_UNAVAILABLE',
  INTERNAL_ERROR: 'INTERNAL_ERROR',
} as const;

export type ErrorCode = (typeof ERROR_CODE)[keyof typeof ERROR_CODE];

export const REJECTION_REASON = {
  TOO_EASY: 'too_easy',
  TOO_HARD: 'too_hard',
  INCORRECT: 'incorrect',
  DUPLICATE: 'duplicate',
  OFF_TOPIC: 'off_topic',
  OTHER: 'other',
} as const;

export type RejectionReason = (typeof REJECTION_REASON)[keyof typeof REJECTION_REASON];

export const MEDIA_KIND = {
  IMAGE: 'image',
  AUDIO: 'audio',
} as const;

export type MediaKind = (typeof MEDIA_KIND)[keyof typeof MEDIA_KIND];

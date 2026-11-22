export const QUERY_KEY = {
  GENERATION: 'generation',
  SYSTEM: 'system',
} as const;

export type QueryKeyRoot = (typeof QUERY_KEY)[keyof typeof QUERY_KEY];

export const MUTATION_KEY = {
  CREATE_GENERATION: 'create-generation',
  CANCEL_GENERATION: 'cancel-generation',
  UPLOAD_DOCUMENT: 'upload-document',
  UPLOAD_RECORDING: 'upload-recording',
  FETCH_ARTICLE: 'fetch-article',
  FETCH_RECORDING: 'fetch-recording',
  REGENERATE_NOTE: 'regenerate-note',
} as const;

export type MutationKeyName = (typeof MUTATION_KEY)[keyof typeof MUTATION_KEY];

export const queryKeys = {
  generation: {
    all: (): readonly [typeof QUERY_KEY.GENERATION] => [QUERY_KEY.GENERATION] as const,
    job: (jobId: string): readonly [typeof QUERY_KEY.GENERATION, 'job', string] =>
      [QUERY_KEY.GENERATION, 'job', jobId] as const,
  },
  system: {
    all: (): readonly [typeof QUERY_KEY.SYSTEM] => [QUERY_KEY.SYSTEM] as const,
    health: (): readonly [typeof QUERY_KEY.SYSTEM, 'health'] =>
      [QUERY_KEY.SYSTEM, 'health'] as const,
  },
} as const;

export const mutationKeys = {
  createGeneration: (): readonly [typeof MUTATION_KEY.CREATE_GENERATION] =>
    [MUTATION_KEY.CREATE_GENERATION] as const,
  cancelGeneration: (jobId: string): readonly [typeof MUTATION_KEY.CANCEL_GENERATION, string] =>
    [MUTATION_KEY.CANCEL_GENERATION, jobId] as const,
  uploadDocument: (): readonly [typeof MUTATION_KEY.UPLOAD_DOCUMENT] =>
    [MUTATION_KEY.UPLOAD_DOCUMENT] as const,
  uploadRecording: (): readonly [typeof MUTATION_KEY.UPLOAD_RECORDING] =>
    [MUTATION_KEY.UPLOAD_RECORDING] as const,
  fetchArticle: (): readonly [typeof MUTATION_KEY.FETCH_ARTICLE] =>
    [MUTATION_KEY.FETCH_ARTICLE] as const,
  fetchRecording: (): readonly [typeof MUTATION_KEY.FETCH_RECORDING] =>
    [MUTATION_KEY.FETCH_RECORDING] as const,
  regenerateNote: (clientId: string): readonly [typeof MUTATION_KEY.REGENERATE_NOTE, string] =>
    [MUTATION_KEY.REGENERATE_NOTE, clientId] as const,
} as const;

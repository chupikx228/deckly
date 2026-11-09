import type { z } from 'zod';

import type {
  Difficulty,
  DocumentUnit,
  ErrorCode,
  HealthStatus,
  JobStage,
  JobStatus,
  MediaKind,
  NoteType,
  RejectionReason,
} from './enums';
import type { components } from './generated/schema';
import type {
  attributionSchema,
  generatedDeckSchema,
  generatedNoteSchema,
  generationJobCreatedSchema,
  generationJobSchema,
  generationRequestSchema,
  generationResultSchema,
  healthSchema,
  mediaSchema,
  documentSourceSchema,
  problemSchema,
  quotaSchema,
  regenerateNoteRequestSchema,
  sourceSchema,
  transcriptSourceSchema,
  webSourceSchema,
} from './schemas';

type MissingKeys<Actual, Expected> = Exclude<keyof Expected, keyof Actual>;
type ExtraKeys<Actual, Expected> = Exclude<keyof Actual, keyof Expected>;

type Expect<Matches extends true> = Matches;

type SameShape<Actual, Expected> = [
  MissingKeys<Actual, Expected>,
  ExtraKeys<Actual, Expected>,
] extends [never, never]
  ? true
  : {
      missingFromZodSchema: MissingKeys<Actual, Expected>;
      absentFromOpenApi: ExtraKeys<Actual, Expected>;
    };

type SameMembers<Actual, Expected> = [
  Exclude<Expected, Actual>,
  Exclude<Actual, Expected>,
] extends [never, never]
  ? true
  : {
      missingFromConstant: Exclude<Expected, Actual>;
      absentFromOpenApi: Exclude<Actual, Expected>;
    };

type Wire<Name extends keyof components['schemas']> = components['schemas'][Name];

export type AssertProblem = Expect<SameShape<z.infer<typeof problemSchema>, Wire<'Problem'>>>;
export type AssertQuota = Expect<SameShape<z.infer<typeof quotaSchema>, Wire<'Quota'>>>;
export type AssertSource = Expect<SameShape<z.infer<typeof sourceSchema>, Wire<'Source'>>>;
export type AssertWebSource = Expect<SameShape<z.infer<typeof webSourceSchema>, Wire<'WebSource'>>>;
export type AssertDocumentSource = Expect<
  SameShape<z.infer<typeof documentSourceSchema>, Wire<'DocumentSource'>>
>;
export type AssertTranscriptSource = Expect<
  SameShape<z.infer<typeof transcriptSourceSchema>, Wire<'TranscriptSource'>>
>;
export type AssertAttribution = Expect<
  SameShape<z.infer<typeof attributionSchema>, Wire<'Attribution'>>
>;
export type AssertMedia = Expect<SameShape<z.infer<typeof mediaSchema>, Wire<'Media'>>>;
export type AssertGeneratedNote = Expect<
  SameShape<z.infer<typeof generatedNoteSchema>, Wire<'GeneratedNote'>>
>;
export type AssertGeneratedDeck = Expect<
  SameShape<z.infer<typeof generatedDeckSchema>, Wire<'GeneratedDeck'>>
>;
export type AssertGenerationResult = Expect<
  SameShape<z.infer<typeof generationResultSchema>, Wire<'GenerationResult'>>
>;
export type AssertGenerationJob = Expect<
  SameShape<z.infer<typeof generationJobSchema>, Wire<'GenerationJob'>>
>;
export type AssertGenerationJobCreated = Expect<
  SameShape<z.infer<typeof generationJobCreatedSchema>, Wire<'GenerationJobCreated'>>
>;
export type AssertHealth = Expect<SameShape<z.infer<typeof healthSchema>, Wire<'Health'>>>;
export type AssertGenerationRequest = Expect<
  SameShape<z.input<typeof generationRequestSchema>, Wire<'GenerationRequest'>>
>;
export type AssertRegenerateNoteRequest = Expect<
  SameShape<z.input<typeof regenerateNoteRequestSchema>, Wire<'RegenerateNoteRequest'>>
>;

export type AssertNoteTypeMembers = Expect<SameMembers<NoteType, Wire<'NoteType'>>>;
export type AssertDifficultyMembers = Expect<SameMembers<Difficulty, Wire<'Difficulty'>>>;
export type AssertJobStatusMembers = Expect<SameMembers<JobStatus, Wire<'JobStatus'>>>;
export type AssertJobStageMembers = Expect<SameMembers<JobStage, Wire<'JobStage'>>>;
export type AssertErrorCodeMembers = Expect<SameMembers<ErrorCode, Wire<'ErrorCode'>>>;
export type AssertRejectionReasonMembers = Expect<
  SameMembers<RejectionReason, Wire<'RejectionReason'>>
>;
export type AssertMediaKindMembers = Expect<SameMembers<MediaKind, Wire<'Media'>['kind']>>;
export type AssertHealthStatusMembers = Expect<SameMembers<HealthStatus, Wire<'Health'>['status']>>;
export type AssertDocumentUnitMembers = Expect<
  SameMembers<DocumentUnit, Wire<'DocumentSource'>['unit']>
>;

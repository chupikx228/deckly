import type { z } from 'zod';

import type { components } from './generated/schema';
import type {
  generatedDeckSchema,
  generatedNoteSchema,
  generationJobCreatedSchema,
  generationJobSchema,
  generationRequestSchema,
  generationResultSchema,
  healthSchema,
  mediaSchema,
  problemSchema,
  quotaSchema,
  regenerateNoteRequestSchema,
  sourceSchema,
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

type Wire<Name extends keyof components['schemas']> = components['schemas'][Name];

export type AssertProblem = Expect<SameShape<z.infer<typeof problemSchema>, Wire<'Problem'>>>;
export type AssertQuota = Expect<SameShape<z.infer<typeof quotaSchema>, Wire<'Quota'>>>;
export type AssertSource = Expect<SameShape<z.infer<typeof sourceSchema>, Wire<'Source'>>>;
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

import { z } from 'zod';

import {
  DIFFICULTY,
  ERROR_CODE,
  JOB_STAGE,
  JOB_STATUS,
  MEDIA_KIND,
  NOTE_TYPE,
  REJECTION_REASON,
} from './enums';

export const noteTypeSchema = z.enum(NOTE_TYPE);
export const difficultySchema = z.enum(DIFFICULTY);
export const jobStatusSchema = z.enum(JOB_STATUS);
export const jobStageSchema = z.enum(JOB_STAGE);
export const errorCodeSchema = z.enum(ERROR_CODE);
export const rejectionReasonSchema = z.enum(REJECTION_REASON);
export const mediaKindSchema = z.enum(MEDIA_KIND);

export const problemSchema = z.object({
  type: z.string(),
  title: z.string(),
  status: z.number().int(),
  detail: z.string().optional(),
  code: errorCodeSchema,
  retryAfterSeconds: z.number().int().optional(),
});

export const sourceSchema = z.object({
  title: z.string(),
  url: z.url(),
  retrievedAt: z.iso.datetime().optional(),
});

export const mediaSchema = z.object({
  mediaId: z.uuid(),
  kind: mediaKindSchema,
  url: z.url(),
  alt: z.string().optional(),
  width: z.number().int().positive().optional(),
  height: z.number().int().positive().optional(),
  license: z.string(),
});

export const generatedNoteSchema = z.object({
  clientId: z.uuid(),
  noteType: noteTypeSchema,
  fields: z.record(z.string(), z.unknown()),
  media: z.array(mediaSchema).default([]),
  sources: z.array(sourceSchema).default([]),
  tags: z.array(z.string()).default([]),
});

export const generatedDeckSchema = z.object({
  title: z.string().max(120),
  description: z.string().max(500).optional(),
  tags: z.array(z.string()).default([]),
});

export const generationResultSchema = z.object({
  deck: generatedDeckSchema,
  notes: z.array(generatedNoteSchema),
});

export const generationJobSchema = z.object({
  jobId: z.uuid(),
  status: jobStatusSchema,
  stage: jobStageSchema.nullable().default(null),
  progress: z.number().min(0).max(1),
  createdAt: z.iso.datetime(),
  updatedAt: z.iso.datetime(),
  result: generationResultSchema.nullable().default(null),
  error: problemSchema.nullable().default(null),
});

export const quotaSchema = z.object({
  limit: z.number().int().nonnegative(),
  remaining: z.number().int().nonnegative(),
  resetsAt: z.iso.datetime(),
});

export const generationJobCreatedSchema = z.object({
  jobId: z.uuid(),
  status: jobStatusSchema,
  createdAt: z.iso.datetime(),
  quota: quotaSchema.optional(),
});

export const healthSchema = z.object({
  status: z.enum(['ok', 'degraded']),
  version: z.string(),
  quota: quotaSchema.optional(),
});

export const generationRequestSchema = z.object({
  topic: z.string().min(3).max(200),
  language: z.string().min(2),
  cardCount: z.number().int().min(5).max(200),
  difficulty: difficultySchema.optional(),
  noteTypes: z.array(noteTypeSchema).min(1).optional(),
  includeImages: z.boolean().optional(),
  instructions: z.string().max(500).optional(),
});

export const regenerateNoteRequestSchema = z.object({
  topic: z.string().min(3).max(200),
  language: z.string().min(2),
  noteType: noteTypeSchema,
  rejectedNote: z.object({
    fields: z.record(z.string(), z.unknown()),
  }),
  reason: rejectionReasonSchema.optional(),
});

export type Problem = z.infer<typeof problemSchema>;
export type Quota = z.infer<typeof quotaSchema>;
export type Source = z.infer<typeof sourceSchema>;
export type Media = z.infer<typeof mediaSchema>;
export type GeneratedNote = z.infer<typeof generatedNoteSchema>;
export type GeneratedDeck = z.infer<typeof generatedDeckSchema>;
export type GenerationResult = z.infer<typeof generationResultSchema>;
export type GenerationJob = z.infer<typeof generationJobSchema>;
export type GenerationJobCreated = z.infer<typeof generationJobCreatedSchema>;
export type Health = z.infer<typeof healthSchema>;
export type GenerationRequest = z.input<typeof generationRequestSchema>;
export type RegenerateNoteRequest = z.input<typeof regenerateNoteRequestSchema>;

import type {
  GeneratedNote,
  GenerationJob,
  GenerationJobCreated,
  GenerationRequest,
  Health,
  RegenerateNoteRequest,
} from '@deckly/api-contract';

export interface GenerationTransport {
  createGeneration(
    request: GenerationRequest,
    idempotencyKey: string,
  ): Promise<GenerationJobCreated>;
  getGeneration(jobId: string): Promise<GenerationJob>;
  cancelGeneration(jobId: string): Promise<void>;
  regenerateNote(request: RegenerateNoteRequest): Promise<GeneratedNote>;
  getHealth(): Promise<Health>;
}

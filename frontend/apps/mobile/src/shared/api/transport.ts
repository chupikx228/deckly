import type {
  Document,
  GeneratedNote,
  GenerationJob,
  GenerationJobCreated,
  GenerationRequest,
  Health,
  RegenerateNoteRequest,
} from '@deckly/api-contract';

export interface DocumentUpload {
  readonly uri: string;
  readonly name: string;
  readonly mimeType: string;
  readonly sizeBytes: number;
}

export type UploadProgressHandler = (ratio: number) => void;

export interface GenerationTransport {
  uploadDocument(file: DocumentUpload, onProgress: UploadProgressHandler): Promise<Document>;
  createGeneration(
    request: GenerationRequest,
    idempotencyKey: string,
  ): Promise<GenerationJobCreated>;
  getGeneration(jobId: string): Promise<GenerationJob>;
  cancelGeneration(jobId: string): Promise<void>;
  regenerateNote(request: RegenerateNoteRequest): Promise<GeneratedNote>;
  getHealth(): Promise<Health>;
}

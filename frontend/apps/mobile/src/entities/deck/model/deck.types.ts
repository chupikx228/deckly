export interface DeckSummary {
  readonly id: string;
  readonly title: string;
  readonly description: string | null;
  readonly updatedAt: number;
  readonly totalCards: number;
  readonly dueCards: number;
}

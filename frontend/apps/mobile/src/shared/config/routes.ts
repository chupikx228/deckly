export const ROUTE = {
  HOME: '/',
  CREATE: '/create',
  DECKS: '/decks',
  PROFILE: '/profile',
  DECK_DETAIL: '/deck/[deckId]',
  NOTE_EDITOR: '/deck/[deckId]/note/[noteId]',
  GENERATION: '/generation/[jobId]',
  STUDY: '/study/[deckId]',
  SESSION_SUMMARY: '/study/[deckId]/summary',
  SETTINGS: '/settings',
  NOT_FOUND: '/+not-found',
} as const;

export type RoutePattern = (typeof ROUTE)[keyof typeof ROUTE];

export const NEW_NOTE_ID = 'new';

export type DeckDetailHref = `/deck/${string}`;
export type NoteEditorHref = `/deck/${string}/note/${string}`;
export type GenerationHref = `/generation/${string}`;
export type StudyHref = `/study/${string}`;
export type SessionSummaryHref = `/study/${string}/summary`;

export const routes = {
  home: (): typeof ROUTE.HOME => ROUTE.HOME,
  create: (): typeof ROUTE.CREATE => ROUTE.CREATE,
  decks: (): typeof ROUTE.DECKS => ROUTE.DECKS,
  profile: (): typeof ROUTE.PROFILE => ROUTE.PROFILE,
  settings: (): typeof ROUTE.SETTINGS => ROUTE.SETTINGS,
  deckDetail: (deckId: string): DeckDetailHref => `/deck/${deckId}`,
  noteEditor: (deckId: string, noteId: string): NoteEditorHref => `/deck/${deckId}/note/${noteId}`,
  newNote: (deckId: string): NoteEditorHref => `/deck/${deckId}/note/${NEW_NOTE_ID}`,
  generation: (jobId: string): GenerationHref => `/generation/${jobId}`,
  study: (deckId: string): StudyHref => `/study/${deckId}`,
  sessionSummary: (deckId: string): SessionSummaryHref => `/study/${deckId}/summary`,
} as const;

export const ROUTE_PARAM = {
  DECK_ID: 'deckId',
  NOTE_ID: 'noteId',
  JOB_ID: 'jobId',
} as const;

export type RouteParamName = (typeof ROUTE_PARAM)[keyof typeof ROUTE_PARAM];

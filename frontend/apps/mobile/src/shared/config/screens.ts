import { ROUTE, type RoutePattern } from './routes';

export const SCREEN = {
  HOME: 'home',
  CREATE: 'create',
  DECKS: 'decks',
  PROFILE: 'profile',
  DECK_DETAIL: 'deck-detail',
  NOTE_EDITOR: 'note-editor',
  GENERATION: 'generation',
  STUDY: 'study',
  SESSION_SUMMARY: 'session-summary',
  SETTINGS: 'settings',
  NOT_FOUND: 'not-found',
} as const;

export type ScreenId = (typeof SCREEN)[keyof typeof SCREEN];

export type NavigationTitleKey = `navigation:${string}`;

export type TabIconName = 'home' | 'sparkles' | 'layers' | 'user';

export interface ScreenDescriptor {
  readonly id: ScreenId;
  readonly pattern: RoutePattern;
  readonly titleKey: NavigationTitleKey;
  readonly presentation: 'card' | 'modal' | 'fullScreenModal';
  readonly hidesTabBar: boolean;
}

export const SCREEN_REGISTRY: Readonly<Record<ScreenId, ScreenDescriptor>> = {
  [SCREEN.HOME]: {
    id: SCREEN.HOME,
    pattern: ROUTE.HOME,
    titleKey: 'navigation:screens.home',
    presentation: 'card',
    hidesTabBar: false,
  },
  [SCREEN.CREATE]: {
    id: SCREEN.CREATE,
    pattern: ROUTE.CREATE,
    titleKey: 'navigation:screens.create',
    presentation: 'card',
    hidesTabBar: false,
  },
  [SCREEN.DECKS]: {
    id: SCREEN.DECKS,
    pattern: ROUTE.DECKS,
    titleKey: 'navigation:screens.decks',
    presentation: 'card',
    hidesTabBar: false,
  },
  [SCREEN.PROFILE]: {
    id: SCREEN.PROFILE,
    pattern: ROUTE.PROFILE,
    titleKey: 'navigation:screens.profile',
    presentation: 'card',
    hidesTabBar: false,
  },
  [SCREEN.DECK_DETAIL]: {
    id: SCREEN.DECK_DETAIL,
    pattern: ROUTE.DECK_DETAIL,
    titleKey: 'navigation:screens.deckDetail',
    presentation: 'card',
    hidesTabBar: true,
  },
  [SCREEN.NOTE_EDITOR]: {
    id: SCREEN.NOTE_EDITOR,
    pattern: ROUTE.NOTE_EDITOR,
    titleKey: 'navigation:screens.noteEditor',
    presentation: 'modal',
    hidesTabBar: true,
  },
  [SCREEN.GENERATION]: {
    id: SCREEN.GENERATION,
    pattern: ROUTE.GENERATION,
    titleKey: 'navigation:screens.generation',
    presentation: 'fullScreenModal',
    hidesTabBar: true,
  },
  [SCREEN.STUDY]: {
    id: SCREEN.STUDY,
    pattern: ROUTE.STUDY,
    titleKey: 'navigation:screens.study',
    presentation: 'fullScreenModal',
    hidesTabBar: true,
  },
  [SCREEN.SESSION_SUMMARY]: {
    id: SCREEN.SESSION_SUMMARY,
    pattern: ROUTE.SESSION_SUMMARY,
    titleKey: 'navigation:screens.sessionSummary',
    presentation: 'fullScreenModal',
    hidesTabBar: true,
  },
  [SCREEN.SETTINGS]: {
    id: SCREEN.SETTINGS,
    pattern: ROUTE.SETTINGS,
    titleKey: 'navigation:screens.settings',
    presentation: 'card',
    hidesTabBar: true,
  },
  [SCREEN.NOT_FOUND]: {
    id: SCREEN.NOT_FOUND,
    pattern: ROUTE.NOT_FOUND,
    titleKey: 'navigation:screens.notFound',
    presentation: 'card',
    hidesTabBar: true,
  },
};

export const TAB_ORDER = [SCREEN.HOME, SCREEN.CREATE, SCREEN.DECKS, SCREEN.PROFILE] as const;

export type TabScreenId = (typeof TAB_ORDER)[number];

export interface TabDescriptor {
  readonly id: TabScreenId;
  readonly pattern: RoutePattern;
  readonly routeName: string;
  readonly icon: TabIconName;
}

export const TAB_REGISTRY: Readonly<Record<TabScreenId, TabDescriptor>> = {
  [SCREEN.HOME]: {
    id: SCREEN.HOME,
    pattern: ROUTE.HOME,
    routeName: 'index',
    icon: 'home',
  },
  [SCREEN.CREATE]: {
    id: SCREEN.CREATE,
    pattern: ROUTE.CREATE,
    routeName: 'create',
    icon: 'sparkles',
  },
  [SCREEN.DECKS]: {
    id: SCREEN.DECKS,
    pattern: ROUTE.DECKS,
    routeName: 'decks',
    icon: 'layers',
  },
  [SCREEN.PROFILE]: {
    id: SCREEN.PROFILE,
    pattern: ROUTE.PROFILE,
    routeName: 'profile',
    icon: 'user',
  },
};

export const TABS: readonly TabDescriptor[] = TAB_ORDER.map((id) => TAB_REGISTRY[id]);

export const getScreenDescriptor = (id: ScreenId): ScreenDescriptor => SCREEN_REGISTRY[id];

export const toStackRouteName = (pattern: RoutePattern): string => pattern.replace(/^\//, '');

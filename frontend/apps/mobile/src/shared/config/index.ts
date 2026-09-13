export { API, GENERATION, QUERY_DEFAULTS, STUDY_DEFAULTS } from './api';

export { TAB_BAR_TINT } from './colors';

export { FONT_FAMILY } from './fonts';

export {
  QUERY_KEY,
  MUTATION_KEY,
  queryKeys,
  mutationKeys,
  type QueryKeyRoot,
  type MutationKeyName,
} from './query-keys';

export {
  ROUTE,
  ROUTE_PARAM,
  NEW_NOTE_ID,
  routes,
  type RoutePattern,
  type RouteParamName,
  type DeckDetailHref,
  type NoteEditorHref,
  type GenerationHref,
  type StudyHref,
  type SessionSummaryHref,
} from './routes';

export {
  SCREEN,
  SCREEN_REGISTRY,
  TAB_ORDER,
  TAB_REGISTRY,
  TABS,
  getScreenDescriptor,
  toStackRouteName,
  type ScreenId,
  type ScreenDescriptor,
  type TabScreenId,
  type TabDescriptor,
  type TabIconName,
  type NavigationTitleKey,
} from './screens';

export {
  LOCALE,
  DEFAULT_LOCALE,
  SUPPORTED_LOCALES,
  I18N_NAMESPACE,
  I18N_NAMESPACES,
  DEFAULT_NAMESPACE,
  isSupportedLocale,
  resolveLocale,
  type Locale,
  type I18nNamespace,
} from './locales';

export {
  NOTE_TYPE,
  CARD_STATE,
  RATING,
  RATING_ORDER,
  DIFFICULTY,
  DIFFICULTY_ORDER,
  SUPPORTED_NOTE_TYPES,
  GENERATABLE_NOTE_TYPES,
  type NoteType,
  type CardState,
  type Rating,
  type Difficulty,
} from './note-types';

export const LOCALE = {
  EN: 'en',
  RU: 'ru',
} as const;

export type Locale = (typeof LOCALE)[keyof typeof LOCALE];

export const DEFAULT_LOCALE: Locale = LOCALE.EN;

export const SUPPORTED_LOCALES: readonly Locale[] = [LOCALE.EN, LOCALE.RU];

export const I18N_NAMESPACE = {
  COMMON: 'common',
  NAVIGATION: 'navigation',
  DECK: 'deck',
  STUDY: 'study',
  GENERATION: 'generation',
  PROFILE: 'profile',
  ERRORS: 'errors',
} as const;

export type I18nNamespace = (typeof I18N_NAMESPACE)[keyof typeof I18N_NAMESPACE];

export const DEFAULT_NAMESPACE: I18nNamespace = I18N_NAMESPACE.COMMON;

export const I18N_NAMESPACES: readonly I18nNamespace[] = [
  I18N_NAMESPACE.COMMON,
  I18N_NAMESPACE.NAVIGATION,
  I18N_NAMESPACE.DECK,
  I18N_NAMESPACE.STUDY,
  I18N_NAMESPACE.GENERATION,
  I18N_NAMESPACE.PROFILE,
  I18N_NAMESPACE.ERRORS,
];

export const isSupportedLocale = (value: string): value is Locale =>
  SUPPORTED_LOCALES.some((locale) => locale === value);

export const resolveLocale = (candidates: readonly string[]): Locale => {
  for (const candidate of candidates) {
    const normalized = candidate.split('-')[0]?.toLowerCase();

    if (normalized !== undefined && isSupportedLocale(normalized)) {
      return normalized;
    }
  }

  return DEFAULT_LOCALE;
};

import { SUPPORTED_LOCALES, type Locale } from '@/shared/config';
import { THEME_PREFERENCE, type ThemePreference } from '@/shared/model';

export const THEME_OPTIONS = [
  THEME_PREFERENCE.SYSTEM,
  THEME_PREFERENCE.LIGHT,
  THEME_PREFERENCE.DARK,
] as const;

export const THEME_LABEL_KEY = {
  [THEME_PREFERENCE.SYSTEM]: 'settings.themeSystem',
  [THEME_PREFERENCE.LIGHT]: 'settings.themeLight',
  [THEME_PREFERENCE.DARK]: 'settings.themeDark',
} as const satisfies Record<ThemePreference, string>;

export const LANGUAGE_OPTIONS: readonly Locale[] = SUPPORTED_LOCALES;

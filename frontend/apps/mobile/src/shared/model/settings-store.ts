import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';

import { DEFAULT_LOCALE, type Locale } from '@/shared/config';
import { STORAGE_KEY, zustandStorage } from '@/shared/lib';

export const THEME_PREFERENCE = {
  SYSTEM: 'system',
  LIGHT: 'light',
  DARK: 'dark',
} as const;

export type ThemePreference = (typeof THEME_PREFERENCE)[keyof typeof THEME_PREFERENCE];

interface SettingsState {
  readonly theme: ThemePreference;
  readonly locale: Locale | null;
  readonly contentLanguage: Locale | null;
  setTheme: (theme: ThemePreference) => void;
  setLocale: (locale: Locale) => void;
  setContentLanguage: (locale: Locale) => void;
}

export const useSettingsStore = create<SettingsState>()(
  persist(
    (set) => ({
      theme: THEME_PREFERENCE.SYSTEM,
      locale: null,
      contentLanguage: null,
      setTheme: (theme) => {
        set({ theme });
      },
      setLocale: (locale) => {
        set({ locale });
      },
      setContentLanguage: (contentLanguage) => {
        set({ contentLanguage });
      },
    }),
    {
      name: STORAGE_KEY.SETTINGS,
      storage: createJSONStorage(() => zustandStorage),
      partialize: (state) => ({
        theme: state.theme,
        locale: state.locale,
        contentLanguage: state.contentLanguage,
      }),
    },
  ),
);

export const selectTheme = (state: SettingsState): ThemePreference => state.theme;
export const selectLocale = (state: SettingsState): Locale => state.locale ?? DEFAULT_LOCALE;

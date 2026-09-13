import { useColorScheme } from 'nativewind';
import type { ReactNode } from 'react';
import { useEffect } from 'react';

import { THEME_PREFERENCE, useSettingsStore } from '@/shared/model';

export interface ThemeProviderProps {
  children: ReactNode;
}

export const ThemeProvider = ({ children }: ThemeProviderProps) => {
  const theme = useSettingsStore((state) => state.theme);
  const { setColorScheme } = useColorScheme();

  useEffect(() => {
    setColorScheme(theme === THEME_PREFERENCE.SYSTEM ? 'system' : theme);
  }, [setColorScheme, theme]);

  return children;
};

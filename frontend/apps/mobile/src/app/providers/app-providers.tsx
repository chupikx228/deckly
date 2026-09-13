import type { ReactNode } from 'react';
import { GestureHandlerRootView } from 'react-native-gesture-handler';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { DatabaseProvider } from './database-provider';
import { I18nProvider } from './i18n-provider';
import { QueryProvider } from './query-provider';
import { ThemeProvider } from './theme-provider';

export interface AppProvidersProps {
  children: ReactNode;
}

export const AppProviders = ({ children }: AppProvidersProps) => (
  <GestureHandlerRootView className="flex-1">
    <SafeAreaProvider>
      <ThemeProvider>
        <I18nProvider>
          <QueryProvider>
            <DatabaseProvider>{children}</DatabaseProvider>
          </QueryProvider>
        </I18nProvider>
      </ThemeProvider>
    </SafeAreaProvider>
  </GestureHandlerRootView>
);

import { useMigrations } from 'drizzle-orm/expo-sqlite/migrator';
import * as SplashScreen from 'expo-splash-screen';
import type { ReactNode } from 'react';
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { db, migrations } from '@/shared/db';
import { EmptyState, Text } from '@/shared/ui';

void SplashScreen.preventAutoHideAsync();

export interface DatabaseProviderProps {
  children: ReactNode;
}

export const DatabaseProvider = ({ children }: DatabaseProviderProps) => {
  const { t } = useTranslation('errors');
  const { success, error } = useMigrations(db, migrations);

  useEffect(() => {
    if (success || error !== undefined) {
      void SplashScreen.hideAsync();
    }
  }, [success, error]);

  if (error !== undefined) {
    return (
      <View className="flex-1 items-center justify-center bg-background">
        <EmptyState title={t('generic.title')} description={t('generic.description')} />
        <Text variant="caption" className="px-gutter text-center">
          {error.message}
        </Text>
      </View>
    );
  }

  if (!success) {
    return null;
  }

  return children;
};

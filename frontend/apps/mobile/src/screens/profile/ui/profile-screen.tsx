import { useRouter } from 'expo-router';
import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';

import { routes } from '@/shared/config';
import { Button, Card, Screen, Text } from '@/shared/ui';

export const ProfileScreen = () => {
  const { t } = useTranslation('profile');
  const router = useRouter();

  const handleOpenSettings = useCallback(() => {
    router.push(routes.settings());
  }, [router]);

  return (
    <Screen scrollable>
      <Text variant="display">{t('stats.title')}</Text>

      <Card>
        <Text variant="heading">{t('stats.streak', { count: 0 })}</Text>
        <Text variant="body" muted className="mt-1">
          {t('stats.reviewedToday', { count: 0 })}
        </Text>
      </Card>

      <Button label={t('settings.title')} variant="secondary" onPress={handleOpenSettings} />
    </Screen>
  );
};

import { useRouter } from 'expo-router';
import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';

import { BackHeader, Card, Screen, Text } from '@/shared/ui';

export const SettingsScreen = () => {
  const { t } = useTranslation('profile');
  const { t: tCommon } = useTranslation('common');
  const router = useRouter();

  const goBack = useCallback(() => {
    router.back();
  }, [router]);

  return (
    <Screen
      scrollable
      header={<BackHeader onBack={goBack} accessibilityLabel={tCommon('actions.back')} />}
    >
      <Text variant="display">{t('settings.title')}</Text>

      <Card>
        <Text variant="label">{t('settings.appearance')}</Text>
        <Text variant="body" muted className="mt-1">
          {t('settings.theme')}
        </Text>
      </Card>

      <Card>
        <Text variant="label">{t('settings.language')}</Text>
        <Text variant="body" muted className="mt-1">
          {t('settings.contentLanguage')}
        </Text>
      </Card>
    </Screen>
  );
};

import { useRouter } from 'expo-router';
import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';

import { routes } from '@/shared/config';
import { EmptyState, Screen } from '@/shared/ui';

export const NotFoundScreen = () => {
  const { t } = useTranslation('navigation');
  const { t: tCommon } = useTranslation('common');
  const router = useRouter();

  const handleGoHome = useCallback(() => {
    router.replace(routes.home());
  }, [router]);

  return (
    <Screen>
      <EmptyState
        title={t('screens.notFound')}
        description={t('screens.home')}
        actionLabel={tCommon('actions.back')}
        onAction={handleGoHome}
      />
    </Screen>
  );
};

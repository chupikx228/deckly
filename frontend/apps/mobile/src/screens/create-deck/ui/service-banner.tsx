import { HEALTH_STATUS } from '@deckly/api-contract';
import { useTranslation } from 'react-i18next';

import { useServiceHealth } from '@/entities/generation';
import { Card, Text } from '@/shared/ui';

export const ServiceBanner = () => {
  const { t } = useTranslation('errors');
  const { data: health, isError } = useServiceHealth();

  const isUnavailable = isError;
  const isDegraded = !isError && health?.status === HEALTH_STATUS.DEGRADED;

  if (!isUnavailable && !isDegraded) {
    return null;
  }

  return (
    <Card className={isUnavailable ? 'border border-danger/40' : 'border border-warning/40'}>
      <Text variant="label" className={isUnavailable ? 'text-danger' : 'text-warning'}>
        {isUnavailable ? t('service.unavailable') : t('service.degraded')}
      </Text>
    </Card>
  );
};

import { useLocalSearchParams, useRouter } from 'expo-router';
import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import {
  selectAccuracy,
  selectDurationMs,
  selectReviewedCount,
  useSessionStore,
} from '@/features/study-session';
import { ROUTE_PARAM, routes } from '@/shared/config';
import { describeInterval, formatPercent, useLocale } from '@/shared/lib';
import { Button, Card, Screen, Text } from '@/shared/ui';

const UNIT_KEY = {
  minutes: 'units.minutes',
  hours: 'units.hours',
  days: 'units.days',
  months: 'units.months',
  years: 'units.years',
} as const;

export const SessionSummaryScreen = () => {
  const { t } = useTranslation('study');
  const { t: tCommon } = useTranslation('common');
  const router = useRouter();
  const locale = useLocale();

  const params = useLocalSearchParams<{ deckId: string }>();
  const deckId = params[ROUTE_PARAM.DECK_ID];

  const reviewed = useSessionStore(selectReviewedCount);
  const accuracy = useSessionStore(selectAccuracy);
  const durationMs = useSessionStore(selectDurationMs);

  const duration = describeInterval(durationMs);

  const handleBack = useCallback(() => {
    router.replace(routes.deckDetail(deckId));
  }, [deckId, router]);

  return (
    <Screen scrollable>
      <Text variant="display">{t('summary.title')}</Text>

      <Card>
        <View className="gap-2">
          <Text variant="heading">{t('summary.reviewed', { count: reviewed })}</Text>

          {accuracy === null ? null : (
            <Text variant="body" muted>
              {t('summary.accuracy', { value: formatPercent(accuracy, locale) })}
            </Text>
          )}

          <Text variant="body" muted>
            {t('summary.duration', {
              value: tCommon(UNIT_KEY[duration.unit], { count: duration.count }),
            })}
          </Text>
        </View>
      </Card>

      <Button label={t('summary.backToDeck')} onPress={handleBack} />
    </Screen>
  );
};

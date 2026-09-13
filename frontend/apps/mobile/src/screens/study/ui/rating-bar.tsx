import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Pressable, View } from 'react-native';

import { RATING, RATING_ORDER, type Rating } from '@/shared/config';
import { describeInterval, type IntervalUnit } from '@/shared/lib';
import { Text } from '@/shared/ui';

const UNIT_KEY = {
  minutes: 'units.minutes',
  hours: 'units.hours',
  days: 'units.days',
  months: 'units.months',
  years: 'units.years',
} as const satisfies Record<IntervalUnit, string>;

const RATING_LABEL_KEY = {
  [RATING.AGAIN]: 'rating.again',
  [RATING.HARD]: 'rating.hard',
  [RATING.GOOD]: 'rating.good',
  [RATING.EASY]: 'rating.easy',
} as const satisfies Record<Rating, string>;

const RATING_CLASS = {
  [RATING.AGAIN]: 'bg-rating-again',
  [RATING.HARD]: 'bg-rating-hard',
  [RATING.GOOD]: 'bg-rating-good',
  [RATING.EASY]: 'bg-rating-easy',
} as const satisfies Record<Rating, string>;

export interface RatingBarProps {
  intervals: Readonly<Record<Rating, number>>;
  now: number;
  onRate: (rating: Rating) => void;
}

export const RatingBar = ({ intervals, now, onRate }: RatingBarProps) => {
  const { t } = useTranslation('study');
  const { t: tCommon } = useTranslation('common');

  const intervalLabel = useCallback(
    (rating: Rating): string => {
      const { unit, count } = describeInterval(intervals[rating] - now);

      return tCommon(UNIT_KEY[unit], { count });
    },
    [intervals, now, tCommon],
  );

  return (
    <View className="flex-row gap-2">
      {RATING_ORDER.map((rating) => (
        <Pressable
          key={rating}
          accessibilityRole="button"
          accessibilityLabel={t(RATING_LABEL_KEY[rating])}
          onPress={() => {
            onRate(rating);
          }}
          className={[
            'flex-1 items-center justify-center gap-0.5 rounded-2xl py-3 active:opacity-80',
            RATING_CLASS[rating],
          ].join(' ')}
        >
          <Text variant="label" className="text-white">
            {t(RATING_LABEL_KEY[rating])}
          </Text>
          <Text variant="caption" className="text-white/80">
            {intervalLabel(rating)}
          </Text>
        </Pressable>
      ))}
    </View>
  );
};

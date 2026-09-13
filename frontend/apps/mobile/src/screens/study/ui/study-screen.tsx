import * as Haptics from 'expo-haptics';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useCallback, useEffect, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { previewCard } from '@/entities/card';
import {
  loadStudyQueue,
  rateCard,
  selectCurrentCard,
  selectPosition,
  selectTotal,
  useSessionStore,
} from '@/features/study-session';
import { RATING, ROUTE_PARAM, STUDY_DEFAULTS, routes, type Rating } from '@/shared/config';
import { getDayCutoff } from '@/shared/lib';
import { Button, EmptyState, Screen, Text } from '@/shared/ui';

import { getStudyFaces } from '../lib/get-study-faces';
import { FlipCard } from './flip-card';
import { RatingBar } from './rating-bar';

export const StudyScreen = () => {
  const { t } = useTranslation('study');
  const { t: tCommon } = useTranslation('common');
  const router = useRouter();
  const params = useLocalSearchParams<{ deckId: string }>();
  const deckId = params[ROUTE_PARAM.DECK_ID];

  const start = useSessionStore((state) => state.start);
  const reveal = useSessionStore((state) => state.reveal);
  const advance = useSessionStore((state) => state.advance);
  const current = useSessionStore(selectCurrentCard);
  const total = useSessionStore(selectTotal);
  const position = useSessionStore(selectPosition);
  const isRevealed = useSessionStore((state) => state.isRevealed);
  const shownAt = useSessionStore((state) => state.shownAt);
  const finishedAt = useSessionStore((state) => state.finishedAt);
  const startedAt = useSessionStore((state) => state.startedAt);

  useEffect(() => {
    const now = Date.now();

    start(
      deckId,
      loadStudyQueue({
        deckId,
        cutoff: getDayCutoff(now),
        dailyReviewLimit: STUDY_DEFAULTS.DAILY_REVIEW_LIMIT,
        dailyNewLimit: STUDY_DEFAULTS.DAILY_NEW_LIMIT,
      }),
      now,
    );
  }, [deckId, start]);

  useEffect(() => {
    if (finishedAt !== null) {
      router.replace(routes.sessionSummary(deckId));
    }
  }, [deckId, finishedAt, router]);

  const faces = useMemo(() => (current === undefined ? null : getStudyFaces(current)), [current]);

  const intervals = useMemo(
    () => (current === undefined || shownAt === null ? null : previewCard(current.card, shownAt)),
    [current, shownAt],
  );

  const handleReveal = useCallback(() => {
    reveal(Date.now());
  }, [reveal]);

  const handleRate = useCallback(
    (rating: Rating) => {
      if (current === undefined) {
        return;
      }

      const now = Date.now();

      rateCard({
        card: current.card,
        rating,
        now,
        durationMs: shownAt === null ? 0 : now - shownAt,
      });

      void Haptics.impactAsync(
        rating === RATING.AGAIN
          ? Haptics.ImpactFeedbackStyle.Heavy
          : Haptics.ImpactFeedbackStyle.Light,
      );

      advance(rating, now);
    },
    [advance, current, shownAt],
  );

  const handleClose = useCallback(() => {
    router.back();
  }, [router]);

  if (startedAt !== null && total === 0) {
    return (
      <Screen>
        <EmptyState
          title={t('empty.title')}
          description={t('empty.description')}
          actionLabel={tCommon('actions.back')}
          onAction={handleClose}
        />
      </Screen>
    );
  }

  if (current === undefined || faces === null || intervals === null) {
    return <Screen>{null}</Screen>;
  }

  return (
    <Screen edges={['top', 'bottom']}>
      <View className="flex-row items-center justify-between py-3">
        <Text variant="caption">{t('session.progress', { current: position, total })}</Text>
        <Text variant="caption" onPress={handleClose}>
          {tCommon('actions.close')}
        </Text>
      </View>

      <FlipCard
        question={faces.question}
        answer={faces.answer}
        hint={faces.hint}
        isRevealed={isRevealed}
        revealLabel={t('session.showAnswer')}
        onReveal={handleReveal}
      />

      <View className="py-4">
        {isRevealed ? (
          <RatingBar intervals={intervals} now={shownAt ?? 0} onRate={handleRate} />
        ) : (
          <Button label={t('session.showAnswer')} onPress={handleReveal} />
        )}
      </View>
    </Screen>
  );
};

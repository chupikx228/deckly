import { JOB_STATUS } from '@deckly/api-contract';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { ActivityIndicator, View } from 'react-native';

import { isGenerationTimedOut, useGenerationJob } from '@/entities/generation';
import { useCancelGeneration } from '@/features/generate-deck';
import { ROUTE_PARAM, routes } from '@/shared/config';
import { useLocale } from '@/shared/lib';
import { EmptyState, Screen } from '@/shared/ui';

import { GenerationPreview } from './generation-preview';
import { GenerationProgress } from './generation-progress';

export const GenerationScreen = () => {
  const { t } = useTranslation('generation');
  const { t: tCommon } = useTranslation('common');
  const { t: tErrors } = useTranslation('errors');
  const router = useRouter();
  const locale = useLocale();

  const params = useLocalSearchParams<{ jobId: string }>();
  const jobId = params[ROUTE_PARAM.JOB_ID];

  const { data: job, isError } = useGenerationJob(jobId);
  const cancel = useCancelGeneration(jobId);

  const handleClose = useCallback(() => {
    router.back();
  }, [router]);

  const handleSaved = useCallback(
    (deckId: string) => {
      router.replace(routes.deckDetail(deckId));
    },
    [router],
  );

  const handleCancel = useCallback(() => {
    cancel.mutate();
  }, [cancel]);

  if (isError) {
    return (
      <Screen>
        <EmptyState
          title={tErrors('generic.title')}
          description={tErrors('generic.description')}
          actionLabel={tCommon('actions.close')}
          onAction={handleClose}
        />
      </Screen>
    );
  }

  if (job === undefined) {
    return (
      <View className="flex-1 items-center justify-center bg-background">
        <ActivityIndicator />
      </View>
    );
  }

  if (job.status === JOB_STATUS.CANCELLED) {
    return (
      <Screen>
        <EmptyState
          title={t('status.cancelled')}
          description={t('cancel.confirmDescription')}
          actionLabel={tCommon('actions.close')}
          onAction={handleClose}
        />
      </Screen>
    );
  }

  if (job.status === JOB_STATUS.FAILED) {
    return (
      <Screen>
        <EmptyState
          title={tErrors('generic.title')}
          description={tErrors('code.GENERATION_FAILED')}
          actionLabel={tCommon('actions.close')}
          onAction={handleClose}
        />
      </Screen>
    );
  }

  if (job.status !== JOB_STATUS.SUCCEEDED) {
    if (isGenerationTimedOut(job)) {
      return (
        <Screen>
          <EmptyState
            title={tErrors('generic.title')}
            description={tErrors('generation.timeout')}
            actionLabel={tCommon('actions.close')}
            onAction={handleClose}
          />
        </Screen>
      );
    }

    return (
      <GenerationProgress
        job={job}
        isCancelling={cancel.isPending}
        onCancel={handleCancel}
        onBack={handleClose}
      />
    );
  }

  if (job.result === null) {
    return (
      <Screen>
        <EmptyState
          title={t('preview.title')}
          description={t('preview.empty')}
          actionLabel={tCommon('actions.close')}
          onAction={handleClose}
        />
      </Screen>
    );
  }

  return (
    <GenerationPreview
      key={jobId}
      jobId={jobId}
      deck={job.result.deck}
      initialNotes={job.result.notes}
      topic={job.result.deck.title}
      language={locale}
      onSaved={handleSaved}
      onDiscardAll={handleClose}
    />
  );
};

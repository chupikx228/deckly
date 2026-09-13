import { createFsrsScheduler } from '@deckly/srs';

import { STUDY_DEFAULTS } from '@/shared/config';

export const scheduler = createFsrsScheduler({
  requestRetention: STUDY_DEFAULTS.TARGET_RETENTION,
});

import { useTranslation } from 'react-i18next';

import { DEFAULT_LOCALE, isSupportedLocale, type Locale } from '@/shared/config';

export const useLocale = (): Locale => {
  const { i18n } = useTranslation();

  return isSupportedLocale(i18n.language) ? i18n.language : DEFAULT_LOCALE;
};

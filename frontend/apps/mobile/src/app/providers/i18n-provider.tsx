import type { ReactNode } from 'react';
import { Suspense, useEffect, useState } from 'react';
import { I18nextProvider } from 'react-i18next';
import { View } from 'react-native';

import { detectDeviceLocale, i18n, initI18n } from '@/shared/i18n';
import { useSettingsStore } from '@/shared/model';

export interface I18nProviderProps {
  children: ReactNode;
}

export const I18nProvider = ({ children }: I18nProviderProps) => {
  const storedLocale = useSettingsStore((state) => state.locale);
  const [instance] = useState(() => initI18n(storedLocale));

  useEffect(() => {
    const target = storedLocale ?? detectDeviceLocale();

    if (instance.language !== target) {
      void instance.changeLanguage(target);
    }
  }, [instance, storedLocale]);

  return (
    <I18nextProvider i18n={i18n}>
      <Suspense fallback={<View className="flex-1 bg-background" />}>{children}</Suspense>
    </I18nextProvider>
  );
};

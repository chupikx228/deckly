import enCommon from './locales/en/common.json';
import enDeck from './locales/en/deck.json';
import enErrors from './locales/en/errors.json';
import enGeneration from './locales/en/generation.json';
import enNavigation from './locales/en/navigation.json';
import enProfile from './locales/en/profile.json';
import enStudy from './locales/en/study.json';
import ruCommon from './locales/ru/common.json';
import ruDeck from './locales/ru/deck.json';
import ruErrors from './locales/ru/errors.json';
import ruGeneration from './locales/ru/generation.json';
import ruNavigation from './locales/ru/navigation.json';
import ruProfile from './locales/ru/profile.json';
import ruStudy from './locales/ru/study.json';

export const resources = {
  en: {
    common: enCommon,
    navigation: enNavigation,
    deck: enDeck,
    study: enStudy,
    generation: enGeneration,
    profile: enProfile,
    errors: enErrors,
  },
  ru: {
    common: ruCommon,
    navigation: ruNavigation,
    deck: ruDeck,
    study: ruStudy,
    generation: ruGeneration,
    profile: ruProfile,
    errors: ruErrors,
  },
} as const;

declare module 'i18next' {
  interface CustomTypeOptions {
    defaultNS: 'common';
    resources: (typeof resources)['en'];
  }
}

import { useCallback, useState } from 'react';

import { DIFFICULTY, GENERATION, NOTE_TYPE, type Difficulty, type Locale } from '@/shared/config';
import { useLocale } from '@/shared/lib';

import { GENERATION_SOURCE, type GeneratableType, type GenerationSource } from './wizard-options';

export interface WizardFields {
  readonly source: GenerationSource;
  readonly topic: string;
  readonly cardCount: string;
  readonly difficulty: Difficulty;
  readonly noteTypes: readonly GeneratableType[];
  readonly contentLanguage: Locale;
  readonly instructions: string;
  readonly articleUrl: string;
  readonly setSource: (value: GenerationSource) => void;
  readonly setTopic: (value: string) => void;
  readonly setCardCount: (value: string) => void;
  readonly setDifficulty: (value: Difficulty) => void;
  readonly setContentLanguage: (value: Locale) => void;
  readonly setInstructions: (value: string) => void;
  readonly setArticleUrl: (value: string) => void;
  readonly toggleNoteType: (type: GeneratableType) => void;
}

export const useWizardFields = (): WizardFields => {
  const locale = useLocale();

  const [source, setSource] = useState<GenerationSource>(GENERATION_SOURCE.TOPIC);
  const [topic, setTopic] = useState('');
  const [cardCount, setCardCount] = useState<string>(String(GENERATION.DEFAULT_CARD_COUNT));
  const [difficulty, setDifficulty] = useState<Difficulty>(DIFFICULTY.INTERMEDIATE);
  const [noteTypes, setNoteTypes] = useState<readonly GeneratableType[]>([NOTE_TYPE.BASIC]);
  const [contentLanguage, setContentLanguage] = useState<Locale>(locale);
  const [instructions, setInstructions] = useState('');
  const [articleUrl, setArticleUrl] = useState('');

  const toggleNoteType = useCallback((type: GeneratableType) => {
    setNoteTypes((current) =>
      current.includes(type) ? current.filter((item) => item !== type) : [...current, type],
    );
  }, []);

  return {
    source,
    topic,
    cardCount,
    difficulty,
    noteTypes,
    contentLanguage,
    instructions,
    articleUrl,
    setSource,
    setTopic,
    setCardCount,
    setDifficulty,
    setContentLanguage,
    setInstructions,
    setArticleUrl,
    toggleNoteType,
  };
};

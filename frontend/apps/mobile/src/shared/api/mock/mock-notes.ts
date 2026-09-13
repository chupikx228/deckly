import { NOTE_TYPE, type GeneratedNote, type GenerationRequest } from '@deckly/api-contract';

import { createId } from '@/shared/lib';

const CLOZE_YEARS = ['1969', '1789', '1945', '1917', '1492'];

const mockSources = (topic: string) => [
  {
    title: `${topic} — reference`,
    url: 'https://example.com/reference',
    retrievedAt: new Date().toISOString(),
  },
];

const basicNote = (topic: string, index: number): GeneratedNote => ({
  clientId: createId(),
  noteType: NOTE_TYPE.BASIC,
  fields: {
    front: `${topic}: key term ${index}`,
    back: `Definition ${index} explaining the term in the context of ${topic}.`,
  },
  media: [],
  sources: mockSources(topic),
  tags: [],
});

const reversedNote = (topic: string, index: number): GeneratedNote => ({
  clientId: createId(),
  noteType: NOTE_TYPE.BASIC_REVERSED,
  fields: {
    front: `${topic} term ${index}`,
    back: `Translation ${index}`,
  },
  media: [],
  sources: mockSources(topic),
  tags: [],
});

const clozeNote = (topic: string, index: number): GeneratedNote => ({
  clientId: createId(),
  noteType: NOTE_TYPE.CLOZE,
  fields: {
    text: `Fact ${index} about ${topic} is dated {{c1::${
      CLOZE_YEARS[index % CLOZE_YEARS.length] ?? '1969'
    }}}.`,
    extra: `Context for fact ${index}.`,
  },
  media: [],
  sources: mockSources(topic),
  tags: [],
});

const BUILDERS = {
  [NOTE_TYPE.BASIC]: basicNote,
  [NOTE_TYPE.BASIC_REVERSED]: reversedNote,
  [NOTE_TYPE.CLOZE]: clozeNote,
} as const;

type SupportedMockType = keyof typeof BUILDERS;

const isSupported = (value: string): value is SupportedMockType => value in BUILDERS;

export const createMockNotes = (request: GenerationRequest): GeneratedNote[] => {
  const requested = (request.noteTypes ?? [NOTE_TYPE.BASIC]).filter(isSupported);
  const types = requested.length > 0 ? requested : [NOTE_TYPE.BASIC];

  return Array.from({ length: request.cardCount }, (_unused, index) => {
    const type = types[index % types.length] ?? NOTE_TYPE.BASIC;

    return BUILDERS[type](request.topic, index + 1);
  });
};

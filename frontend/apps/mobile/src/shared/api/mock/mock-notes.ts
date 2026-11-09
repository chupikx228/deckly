import {
  DOCUMENT_UNIT,
  NOTE_TYPE,
  SOURCE_KIND,
  type GeneratedNote,
  type GenerationRequest,
  type Source,
} from '@deckly/api-contract';

import { createId } from '@/shared/lib';

const CLOZE_YEARS = ['1969', '1789', '1945', '1917', '1492'];

const MOCK_DOCUMENT_ID = 'b1a2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d';

const MOCK_SOURCE_VARIANTS: readonly ((topic: string) => Source)[] = [
  (topic) => ({
    kind: SOURCE_KIND.WEB,
    title: `${topic} — reference`,
    url: 'https://example.com/reference',
    retrievedAt: new Date().toISOString(),
  }),
  (topic) => ({
    kind: SOURCE_KIND.DOCUMENT,
    title: `${topic}.pdf`,
    documentId: MOCK_DOCUMENT_ID,
    unit: DOCUMENT_UNIT.PAGE,
    number: 12,
  }),
  (topic) => ({
    kind: SOURCE_KIND.TRANSCRIPT,
    title: `${topic}.m4a`,
    documentId: MOCK_DOCUMENT_ID,
    startMs: 62_480,
    endMs: 121_900,
  }),
];

const mockSources = (topic: string, index: number): Source[] => {
  const variant = MOCK_SOURCE_VARIANTS[index % MOCK_SOURCE_VARIANTS.length];

  return variant === undefined ? [] : [variant(topic)];
};

const basicNote = (topic: string, index: number): GeneratedNote => ({
  clientId: createId(),
  noteType: NOTE_TYPE.BASIC,
  fields: {
    front: `${topic}: key term ${index}`,
    back: `Definition ${index} explaining the term in the context of ${topic}.`,
  },
  media: [],
  sources: mockSources(topic, index),
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
  sources: mockSources(topic, index),
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
  sources: mockSources(topic, index),
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

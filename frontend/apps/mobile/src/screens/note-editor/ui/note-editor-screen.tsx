import { useLocalSearchParams, useRouter } from 'expo-router';
import { useCallback } from 'react';
import { ActivityIndicator, View } from 'react-native';

import { useNote } from '@/entities/note';
import { deleteNote, saveNote } from '@/features/edit-note';
import { NEW_NOTE_ID, ROUTE_PARAM } from '@/shared/config';

import {
  EMPTY_NOTE_FORM_VALUES,
  toNoteFormValues,
  type EditableNoteType,
} from '../model/note-form-values';
import { NoteForm } from './note-form';

export const NoteEditorScreen = () => {
  const router = useRouter();
  const params = useLocalSearchParams<{ deckId: string; noteId: string }>();
  const deckId = params[ROUTE_PARAM.DECK_ID];
  const routeNoteId = params[ROUTE_PARAM.NOTE_ID];
  const isNew = routeNoteId === NEW_NOTE_ID;

  const { data: noteRows } = useNote(isNew ? '' : routeNoteId);
  const existing = noteRows[0];

  const handleSave = useCallback(
    (noteType: EditableNoteType, fields: Record<string, unknown>) => {
      saveNote({ noteId: isNew ? null : routeNoteId, deckId, noteType, fields });
      router.back();
    },
    [deckId, isNew, routeNoteId, router],
  );

  const handleDelete = useCallback(() => {
    deleteNote(routeNoteId);
    router.back();
  }, [routeNoteId, router]);

  const handleBack = useCallback(() => {
    router.back();
  }, [router]);

  if (!isNew && existing === undefined) {
    return (
      <View className="flex-1 items-center justify-center bg-background">
        <ActivityIndicator />
      </View>
    );
  }

  return (
    <NoteForm
      key={existing?.id ?? NEW_NOTE_ID}
      initialValues={existing === undefined ? EMPTY_NOTE_FORM_VALUES : toNoteFormValues(existing)}
      isNew={isNew}
      onSave={handleSave}
      onDelete={handleDelete}
      onBack={handleBack}
    />
  );
};

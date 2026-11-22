import type { DocumentContentType } from '@deckly/api-contract';
import * as DocumentPicker from 'expo-document-picker';
import { useCallback, useState } from 'react';

import type { DocumentUpload } from '@/shared/api';

export interface DocumentPick {
  readonly file: DocumentUpload | null;
  readonly pickFile: (allowed: readonly DocumentContentType[]) => void;
  readonly clearFile: () => void;
}

export const useDocumentPick = (): DocumentPick => {
  const [file, setFile] = useState<DocumentUpload | null>(null);

  const pickFile = useCallback((allowed: readonly DocumentContentType[]) => {
    void (async () => {
      const result = await DocumentPicker.getDocumentAsync({
        type: [...allowed],
        copyToCacheDirectory: true,
        multiple: false,
      });

      if (result.canceled) {
        return;
      }

      const [asset] = result.assets;

      if (asset === undefined) {
        return;
      }

      setFile({
        uri: asset.uri,
        name: asset.name,
        mimeType: asset.mimeType ?? '',
        sizeBytes: asset.size ?? 0,
      });
    })();
  }, []);

  const clearFile = useCallback(() => {
    setFile(null);
  }, []);

  return { file, pickFile, clearFile };
};

import { GENERATABLE_DOCUMENT_TYPES } from '@deckly/api-contract';
import * as DocumentPicker from 'expo-document-picker';
import { useCallback, useState } from 'react';

import type { DocumentUpload } from '@/shared/api';

export interface DocumentPick {
  readonly file: DocumentUpload | null;
  readonly pickFile: () => void;
}

export const useDocumentPick = (): DocumentPick => {
  const [file, setFile] = useState<DocumentUpload | null>(null);

  const pickFile = useCallback(() => {
    void (async () => {
      const result = await DocumentPicker.getDocumentAsync({
        type: [...GENERATABLE_DOCUMENT_TYPES],
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

  return { file, pickFile };
};

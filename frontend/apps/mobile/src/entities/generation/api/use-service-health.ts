import { useQuery } from '@tanstack/react-query';

import { generationApi } from '@/shared/api';
import { queryKeys } from '@/shared/config';

export const useServiceHealth = () =>
  useQuery({
    queryKey: queryKeys.system.health(),
    queryFn: () => generationApi.getHealth(),
  });

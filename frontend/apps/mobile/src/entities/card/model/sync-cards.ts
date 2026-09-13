export interface CardOrdinalDiff {
  readonly toCreate: number[];
  readonly toDelete: number[];
  readonly kept: number[];
}

export const diffCardOrdinals = (
  existing: readonly number[],
  next: readonly number[],
): CardOrdinalDiff => {
  const existingSet = new Set(existing);
  const nextSet = new Set(next);

  return {
    toCreate: next.filter((ordinal) => !existingSet.has(ordinal)),
    toDelete: existing.filter((ordinal) => !nextSet.has(ordinal)),
    kept: next.filter((ordinal) => existingSet.has(ordinal)),
  };
};

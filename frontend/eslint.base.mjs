import tseslint from 'typescript-eslint';

export const strictRules = {
  '@typescript-eslint/no-explicit-any': 'error',
  '@typescript-eslint/no-non-null-assertion': 'error',
  '@typescript-eslint/consistent-type-imports': 'error',
  '@typescript-eslint/no-unused-vars': [
    'error',
    {
      argsIgnorePattern: '^_',
      varsIgnorePattern: '^_',
      caughtErrorsIgnorePattern: '^_',
      ignoreRestSiblings: true,
    },
  ],
  complexity: ['error', 12],
  'max-params': ['error', 4],
  'max-depth': ['error', 4],
  'max-nested-callbacks': ['error', 3],
  'max-lines': ['error', { max: 300, skipBlankLines: true, skipComments: true }],
};

export const logicRules = {
  'max-lines-per-function': ['error', { max: 80, skipBlankLines: true, skipComments: true }],
};

export const base = tseslint.config(
  { ignores: ['dist/**', 'src/generated/**', 'node_modules/**'] },
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.ts'],
    rules: { ...strictRules, ...logicRules },
  },
  {
    files: ['**/*.test.ts'],
    rules: { 'max-lines-per-function': 'off', 'max-lines': 'off', 'max-nested-callbacks': 'off' },
  },
);

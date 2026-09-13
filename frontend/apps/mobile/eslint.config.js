const expoConfig = require('eslint-config-expo/flat');
const boundaries = require('eslint-plugin-boundaries');

const LAYERS = ['app', 'screens', 'widgets', 'features', 'entities', 'shared'];

const allowedBelow = (layer) => LAYERS.slice(LAYERS.indexOf(layer) + 1);

const policy = (from, allowed) => ({
  from: [{ element: { type: from } }],
  allow: allowed.map((type) => ({ to: { element: { type } } })),
});

module.exports = [
  ...expoConfig,
  {
    ignores: ['node_modules/**', 'ios/**', 'android/**', '.expo/**', 'expo-env.d.ts'],
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: { boundaries },
    settings: {
      'boundaries/include': ['src/**/*'],
      'boundaries/elements': [
        { type: 'app', pattern: 'src/app/**' },
        { type: 'screens', pattern: 'src/screens/*', capture: ['slice'] },
        { type: 'widgets', pattern: 'src/widgets/*', capture: ['slice'] },
        { type: 'features', pattern: 'src/features/*', capture: ['slice'] },
        { type: 'entities', pattern: 'src/entities/*', capture: ['slice'] },
        { type: 'shared', pattern: 'src/shared/**' },
      ],
    },
    rules: {
      'react/no-unstable-nested-components': ['error', { allowAsProps: true }],
      'no-restricted-syntax': [
        'error',
        {
          selector:
            "CallExpression[callee.type='Identifier'][callee.name=/^use(?!SyncExternalStore).+Store$/][arguments.length=0]",
          message:
            'Select from a store with a narrow selector: useXStore((s) => s.field). Reading the whole store re-renders on every unrelated change.',
        },
        {
          selector:
            "CallExpression[callee.type='Identifier'][callee.name=/^use(?!SyncExternalStore).+Store$/] > ArrowFunctionExpression[body.type='ObjectExpression']",
          message:
            'A store selector that returns an object literal allocates a new object every render. Wrap it in useShallow, or select each field separately.',
        },
      ],
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
      'boundaries/dependencies': [
        'error',
        {
          default: 'disallow',
          policies: [
            ...LAYERS.filter((layer) => layer !== 'shared').map((layer) =>
              policy(layer, allowedBelow(layer)),
            ),
            policy('shared', ['shared']),
          ],
        },
      ],
      'boundaries/entry-point': [
        'error',
        {
          default: 'disallow',
          policies: [
            {
              target: ['screens', 'widgets', 'features', 'entities'].map((type) => ({
                element: { type },
              })),
              allow: ['index.ts'],
            },
            {
              target: ['app', 'shared'].map((type) => ({ element: { type } })),
              allow: ['**'],
            },
          ],
        },
      ],
    },
  },
  {
    files: ['src/**/*.ts'],
    rules: {
      'max-lines-per-function': ['error', { max: 80, skipBlankLines: true, skipComments: true }],
    },
  },
  {
    files: ['src/**/*.{test,spec}.{ts,tsx}'],
    rules: {
      'max-lines-per-function': 'off',
      'max-lines': 'off',
      'max-nested-callbacks': 'off',
    },
  },
];

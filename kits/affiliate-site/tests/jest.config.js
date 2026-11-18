/**
 * Unit tests of the gate engine itself (tests/unit/*.test.ts): the compliance
 * rules, the robots/llms parsers, the runner script, the gate-config loader
 * and the content-policy bridge. Offline and fast; the Playwright specs under
 * tests/ui run only through the gate.
 *
 *   npx jest -c tests/jest.config.js           (from the kit root)
 *
 * Kept apart from the kit's own jest.config.js (roots: unit/), whose suite
 * this does not change.
 *
 * @type {import('jest').Config}
 */
module.exports = {
  rootDir: '..',
  testEnvironment: 'node',
  roots: ['<rootDir>/tests/unit'],
  testMatch: ['**/*.test.ts'],
  moduleFileExtensions: ['ts', 'js', 'json'],
  transform: {
    '^.+\\.ts$': ['ts-jest', { tsconfig: '<rootDir>/tsconfig.jest.json' }],
  },
  clearMocks: true,
  restoreMocks: true,
};

/**
 * The site's unit tests: every *.test.ts under tests/unit, compiled and type-checked by
 * ts-jest against tsconfig.jest.json. Playwright specs (tests/ui) belong to
 * the deploy gate (tests/run-local-suite.sh) and never run under jest.
 *
 * @type {import('jest').Config}
 */
module.exports = {
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

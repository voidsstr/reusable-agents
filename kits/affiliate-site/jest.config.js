/**
 * Kit unit tests: everything under unit/ named *.test.ts, compiled and type-checked by ts-jest
 * against tsconfig.jest.json (strict). Playwright specs (tests/ui/*.spec.ts) are run
 * by the gate engine, never by jest.
 *
 * @type {import('jest').Config}
 */
module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/unit'],
  testMatch: ['**/*.test.ts'],
  moduleFileExtensions: ['ts', 'js', 'json'],
  transform: {
    '^.+\\.ts$': ['ts-jest', { tsconfig: '<rootDir>/tsconfig.jest.json' }],
  },
  clearMocks: true,
  restoreMocks: true,
  collectCoverageFrom: ['server/src/**/*.ts'],
  coverageDirectory: 'coverage',
};

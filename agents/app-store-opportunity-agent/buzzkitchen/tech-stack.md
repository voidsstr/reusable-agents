# BuzzKitchen — Tech Stack

## Mobile (Expo / React Native)

| Package | Version | Purpose |
|---|---|---|
| Expo SDK | 54 | Managed workflow, build tools, native modules |
| React Native | 0.81 | Cross-platform native rendering |
| TypeScript | 5.4 | Type safety across all app code |
| Expo Router | 3.x | File-based navigation (app/ directory) |
| NativeWind | 4.2 | Tailwind class names on React Native components |
| TanStack Query | 5.x | Server state, caching, background refetch |
| Zustand | 4.x | Local UI state (pantry draft, cooking mode) |
| Axios | 1.6 | HTTP client with interceptors for JWT |
| AsyncStorage | 1.21 | Persist JWT and pantry draft locally |
| react-native-youtube-iframe | 2.3 | Embed YouTube player in recipe detail |
| expo-auth-session | 5.x | Google OAuth PKCE flow |
| expo-apple-authentication | 6.x | Apple Sign In (iOS only) |
| expo-notifications | 0.28 | Push notification registration and receipt |
| expo-barcode-scanner | 12.x | Barcode scan for pantry ingredient add |
| EAS Build | latest | Cloud build and code signing |
| EAS Submit | latest | App Store and Play Store submission |

## Web (React / Vite)

| Package | Version | Purpose |
|---|---|---|
| React | 18.2 | UI rendering |
| Vite | 5.2 | Dev server and production bundler |
| TypeScript | 5.4 | Type safety |
| Tailwind CSS | 3.4 | Utility-first styling |
| @headlessui/react | 1.7 | Accessible modals, dropdowns, toggles |
| @heroicons/react | 2.0 | Icon library (SVG) |
| React Router | 6.14 | Client-side routing |
| TanStack Query | 5.x | Server state and caching |
| Axios | 1.6 | HTTP client |
| @react-oauth/google | 0.12 | Google OAuth button and token |

## Backend (Node / Express)

| Package | Version | Purpose |
|---|---|---|
| Node.js | 18 LTS | Runtime |
| Express | 4.18 | HTTP server |
| TypeScript | 5.3 | Type safety |
| Prisma | 5.x | ORM and migration runner |
| PostgreSQL | 15 | Primary database |
| passport-google-oauth20 | 2.0 | Google OAuth strategy |
| jsonwebtoken | 9.0 | JWT sign and verify |
| express-rate-limit | 6.7 | Per-IP rate limiting on auth routes |
| ws | 8.13 | WebSocket server (future live session) |
| zod | 3.x | Request body validation |
| pino | 8.x | Structured JSON logging |
| pino-http | 9.x | HTTP request logging middleware |
| multer | 1.4 | Cook log photo upload |
| sharp | 0.33 | Image resize and WebP conversion before storage |

## Infrastructure

### docker-compose.yml (local development)

```yaml
version: "3.9"
services:
  db:
    image: postgres:15-alpine
    environment:
      POSTGRES_DB: buzzkitchen
      POSTGRES_USER: bk
      POSTGRES_PASSWORD: bk_dev
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

  backend:
    build: .
    depends_on:
      - db
    environment:
      DATABASE_URL: postgresql://bk:bk_dev@db:5432/buzzkitchen
      JWT_SECRET: dev_jwt_secret_change_in_prod
      GOOGLE_CLIENT_ID: ${GOOGLE_CLIENT_ID}
      GOOGLE_CLIENT_SECRET: ${GOOGLE_CLIENT_SECRET}
      AZURE_STORAGE_CONNECTION_STRING: ${AZURE_STORAGE_CONNECTION_STRING}
      PORT: 3001
    ports:
      - "3001:3001"
    volumes:
      - .:/app
    command: npx ts-node-dev src/index.ts

  frontend:
    image: node:18-alpine
    working_dir: /app
    volumes:
      - ./frontend:/app
    ports:
      - "5173:5173"
    command: npm run dev -- --host

volumes:
  pgdata:
```

### Azure Container Apps (production)

Two Container Apps in the `buzzkitchen-rg` resource group:

- `buzzkitchen-backend`: Node/Express image from ACR, min replicas 1, max 10, ingress external port 3001, env vars from Azure Key Vault references.
- `buzzkitchen-frontend`: Nginx serving the Vite build, min replicas 1, max 5.

Azure Database for PostgreSQL Flexible Server (Standard_B2ms, 15.x) in the same VNet. Blob Storage account `buzzkitchenstorage` for cook log photos. CDN profile with custom domain `api.buzzkitchen.app` and `buzzkitchen.app`.

## CI — GitHub Actions

```yaml
name: CI/CD
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  ci:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:15-alpine
        env:
          POSTGRES_DB: buzzkitchen_test
          POSTGRES_USER: bk
          POSTGRES_PASSWORD: bk_test
        ports: ["5432:5432"]

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: "18"
          cache: "npm"

      - run: npm ci
      - run: npm run typecheck
      - run: npm run lint
      - run: npm test
        env:
          DATABASE_URL: postgresql://bk:bk_test@localhost:5432/buzzkitchen_test
          JWT_SECRET: test_secret

      - name: Build Docker image
        run: docker build -t buzzkitchen-backend:${{ github.sha }} .

      - name: Deploy to Azure
        if: github.ref == 'refs/heads/main'
        run: bash azure/deploy.sh
        env:
          AZURE_CREDENTIALS: ${{ secrets.AZURE_CREDENTIALS }}
          ACR_USERNAME: ${{ secrets.ACR_USERNAME }}
          ACR_PASSWORD: ${{ secrets.ACR_PASSWORD }}
```

## Observability

| Tool | Purpose |
|---|---|
| GlitchTip (self-hosted) | Error tracking on mobile (Sentry SDK) and backend (Sentry SDK); crash → alert → auto-fix loop via `crash-watcher-agent` |
| pino-http + Azure Log Analytics | Structured HTTP request logs streamed to Log Analytics workspace |
| Azure Monitor | CPU/memory/replica metrics on Container Apps; alert on p95 latency > 800ms |
| UptimeRobot | External HTTP monitor on `GET /api/health` every 1 minute; alert on 2 consecutive failures |
| Lighthouse CI | Performance budget in CI: LCP ≤ 2.5s, TBT ≤ 200ms, CLS ≤ 0.1 for `/` and `/recipe/:id` |

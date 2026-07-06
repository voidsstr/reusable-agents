# BulkWise -- Tech Stack

## Frontend (Web)

| Package | Version | Role |
|---|---|---|
| react | 18.2 | UI library |
| vite | 5.2 | Build/dev server |
| typescript | 5.4 | Types |
| react-router-dom | 6.14 | Routing |
| @tanstack/react-query | 4.29 | Server-state cache |
| axios | 1.6 | HTTP client |
| @react-oauth/google | 0.12 | Google sign-in |
| @headlessui/react | 1.7 | Accessible primitives |
| @heroicons/react | 2.0 | Icons |
| tailwindcss | 3.4 | Styling |
| recharts | 2.8 | Price-history sparklines |

### State Management

- **Server state:** TanStack Query (caching, background refetch, optimistic updates for list mutations).
- **Client state:** React Context for `auth`, `theme` (light/dark), and `warehouse` (selected home warehouse). No Redux.

## Mobile (Expo / React Native)

| Package | Version | Role |
|---|---|---|
| expo | 54 | RN toolchain |
| react-native | 0.81.5 | Runtime |
| @react-navigation/native | 6 | Navigation |
| nativewind | 4.2 | Tailwind-in-RN styling |
| @react-native-async-storage/async-storage | 2.2 | Local persistence |
| expo-barcode-scanner | latest (SDK 54) | UPC scanning |
| expo-notifications | latest (SDK 54) | Push notifications |
| expo-haptics | latest (SDK 54) | Haptic feedback |

### Offline-First Strategy

- **AsyncStorage first:** lists and deals render from local cache immediately; network is enhancement.
- **Mutation queue:** writes (add/check/delete item) enqueue locally and flush when online.
- **NetInfo gates:** `@react-native-community/netinfo` gates flush + shows offline banner.
- **Conflict policy:** last-write-wins per field, reconciled on reconnect via server timestamps.

### Bundle IDs and EAS

- iOS + Android bundle identifier: `com.bulkwise.app`.
- `eas.json` profiles: `development` (dev client, internal dist), `preview` (internal, release config), `production` (store builds, auto-increment build number).

## Backend

| Package | Version | Role |
|---|---|---|
| node | 18 | Runtime |
| express | 4.18 | HTTP framework |
| typescript | 5.3 | Types |
| prisma | 5 | ORM |
| passport | latest | Auth middleware |
| passport-google-oauth20 | latest | Google OAuth strategy |
| jsonwebtoken | 9 | JWT issue/verify |
| express-rate-limit | 6.7 | Rate limiting |
| ws | 8.13 | WebSocket server |
| zod | 3.22 | Request validation |

### Single-File Server Pattern

The Express app boots from a single `src/index.ts` that wires middleware, mounts routers, attaches the `ws` server to the same HTTP server, and exports the app for tests. Routers live under `src/routes/` and share `src/lib/` (prisma client, jwt, validation, ws hub).

### optionalAuthMiddleware

```ts
import { Request, Response, NextFunction } from "express";
import jwt from "jsonwebtoken";

export interface AuthedRequest extends Request {
  userId?: string;
}

// Attaches userId if a valid token is present, but never rejects.
// Used by [opt] endpoints (warehouses, deals, products) so anonymous
// users get public data while authed users get personalized results.
export function optionalAuthMiddleware(
  req: AuthedRequest,
  _res: Response,
  next: NextFunction
) {
  const header = req.headers.authorization;
  if (header && header.startsWith("Bearer ")) {
    try {
      const payload = jwt.verify(
        header.slice(7),
        process.env.JWT_SECRET as string
      ) as { sub: string };
      req.userId = payload.sub;
    } catch {
      // ignore invalid token; remain anonymous
    }
  }
  next();
}
```

### LLM Routing (AI List Optimizer)

- Routed through the framework `chat_with_fallback` helper; primary model **gpt-4o-mini** for the meal-plan-to-list optimizer (cheap, structured-output friendly).
- Per-user rate limit: **10 requests/min** enforced via `express-rate-limit` keyed on `userId`.
- Output is validated with Zod against the optimizer response schema before returning.

## Database

- **Postgres 15** as the system of record.
- **Prisma 5** for schema, migrations, and the typed client.
- **Local:** Postgres via Docker Compose.
- **Prod:** Azure Database for PostgreSQL Flexible Server.

## Infrastructure

- **docker-compose.yml** services: `db` (postgres:15), `backend` (Node/Express), `frontend` (Vite preview / static).
- **Azure Container Apps:** backend + frontend deployed as Container Apps, **1-3 replicas** with autoscale, **0.5 vCPU / 1Gi** per replica.
- Secrets via Container Apps secrets / Azure Key Vault references; Postgres connection string injected as a secret.

## CI/CD (GitHub Actions)

Workflow outline (`.github/workflows/ci.yml`):

1. **lint** -- ESLint across web/mobile/backend/shared.
2. **typecheck** -- `tsc --noEmit` per package.
3. **test** -- Vitest/Jest unit + API tests against an ephemeral Postgres service.
4. **build-push** (on push to `main`) -- build Docker images (`Dockerfile.azure`), push to `nscappsacr` ACR, update the Container App revision.

## Observability

- **Errors:** GlitchTip (self-hosted Sentry-compatible) SDK in web, mobile, and backend.
- **Logs:** pino JSON structured logs from the backend.
- **Metrics:** Azure Monitor (request rate, latency, replica count).
- **Uptime:** UptimeRobot pinging `/api/health` every minute.

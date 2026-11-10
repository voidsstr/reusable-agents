# BuzzKitchen

A creator-native Japanese recipe app that displaces リュウジのバズレシピ by solving the four gaps driving its 3.5★ rating: no in-app video, no shopping list, no pantry search, and no community cook log.

## How to Read This Repository

Read files in this order:

1. `product-spec.md` — what we are building and why
2. `ux-research.md` — competitor analysis and user pain points
3. `ux-spec.md` — screens, colours, typography
4. `tech-stack.md` — technology decisions and infrastructure
5. `api-spec.md` — every REST endpoint
6. `data-model.md` — Prisma schema and ER diagram
7. `implementation-plan.md` — full file tree and three milestones

## Stack at a Glance

| Layer | Technology |
|---|---|
| Mobile | Expo SDK 54 · React Native 0.81 · TypeScript · NativeWind · Expo Router |
| Web | React 18 · Vite · TypeScript · Tailwind · TanStack Query |
| Backend | Node 18 · Express 4 · TypeScript · Prisma 5 · PostgreSQL 15 |
| Auth | Google OAuth · Apple Sign In → JWT |
| Infra | docker-compose (local) · Azure Container Apps (production) |
| CI | GitHub Actions → lint → typecheck → test → image → deploy |

## File Index

| File | Purpose |
|---|---|
| `README.md` | This file — orientation and stack overview |
| `product-spec.md` | Vision, features, success metrics |
| `ux-research.md` | Competitor gaps, review themes, adjacent apps |
| `ux-spec.md` | Colour palette, typography, screen specs |
| `tech-stack.md` | Dependency tables, docker-compose, CI workflow |
| `api-spec.md` | Full REST endpoint catalog |
| `data-model.md` | Prisma schema + ER diagram |
| `implementation-plan.md` | File tree + M1/M2/M3 milestone commits |

## Quick Start

```bash
cp .env.example .env
docker-compose up -d
npx prisma migrate dev
npx ts-node src/seed.ts
open http://localhost:5173
```

Mobile: `cd mobile && npx expo start`

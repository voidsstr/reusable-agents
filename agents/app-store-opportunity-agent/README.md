# BulkWise

Smart Warehouse Club Shopping Companion.

## Documentation Index

| File | Purpose | Read first? |
|---|---|---|
| README.md | Project overview, file index, architecture summary, north-star metric | Yes |
| product-spec.md | Vision, target user, MVP/v2/v3 features, non-goals, success metrics | Yes |
| ux-research.md | Costco app complaint themes + 3 adjacent-app teardowns + design principles | No |
| ux-spec.md | Color/type tokens, 5 key screen specs, animations, accessibility | No |
| tech-stack.md | Frontend/mobile/backend/db/infra/CI/observability decisions | No |
| api-spec.md | Complete REST + WebSocket endpoint catalog with request/response shapes | No |
| data-model.md | Full Prisma schema, Mermaid ERD, index summary | No |
| implementation-plan.md | File tree, 3 milestones (60 commits), monetization details | No |

## What BulkWise Is

BulkWise is a smart shopping companion for warehouse-club members (starting with Costco) that fixes the everyday friction the official apps ignore. It pairs an offline-first shopping list with real-time unit-price math, a searchable coupon book with push alerts, a community-confirmed gas price finder, and a membership ROI dashboard that shows the exact date your savings pay back your annual fee. Households of up to five members share lists and savings in real time, an AI optimizer turns a meal plan into a Costco run, and price-history sparklines reveal whether today's deal is actually a deal. BulkWise is community-powered (gas confirms, stock reports, deal confirms) rather than scraper-dependent, so the data is fresher than competitors that lag 24-48 hours. It never sells products, takes payments for goods, or scrapes in violation of any retailer's terms.

## Architecture Summary

- **Web:** React 18 + Vite + TypeScript, TanStack Query, Tailwind, Recharts.
- **Mobile:** Expo 54 / React Native 0.81.5, React Navigation 6, NativeWind, AsyncStorage offline cache + mutation queue.
- **Backend:** Node 18 + Express + TypeScript, Prisma 5 over Postgres 15, JWT auth via Google OAuth, WebSocket (`ws`) for real-time list/gas sync.
- **Shared:** `packages/shared` exports TypeScript types, API client, and storage helpers consumed by web + mobile.
- **Infra:** Docker Compose for local (db/backend/frontend); Azure Container Apps (1-3 replicas, 0.5 vCPU / 1Gi) + Azure Postgres Flexible Server for prod; GitHub Actions CI (lint, typecheck, test, build-push on main).
- **Observability:** GlitchTip errors, pino JSON logs, Azure Monitor, UptimeRobot against `/api/health`.

## North-Star Metric

**Weekly Active Members (WAM) >= 10,000 within 6 months of launch.** Every roadmap decision is weighed against whether it grows WAM. Downloads and Premium conversion are leading/lagging supports; WAM is the truth.

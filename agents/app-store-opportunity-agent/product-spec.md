# BulkWise -- Product Specification

## Vision

Warehouse-club shopping is a high-stakes, high-friction errand: large baskets, confusing per-unit pricing, time-limited coupon books, volatile gas prices, and an annual membership fee that members rarely know whether they have earned back. The official Costco app is highly rated but optimized for e-commerce, not the in-warehouse trip. BulkWise is the companion that owns the *trip*: an offline-first list with live unit-price math, a coupon book you can actually search, community-confirmed gas and stock data that beats stale scrapes, and a membership ROI dashboard that proves the value of every visit. We win by being the fastest, calmest, most trustworthy tool a member opens in the parking lot and in the aisle -- and by letting whole households share that value in real time.

## Target User -- The Engaged Costco Member

- **Behavior:** Shops a warehouse club 2-4 times per month; plans trips; tracks spend.
- **Household:** 2-5 people; often coordinates the list with a partner.
- **Age:** 28-52.
- **Income:** $75k+ household; values savings but also values time.
- **Mindset:** Already pays the membership fee and wants to maximize it; frustrated by the official app's navigation, search, and gas UX; keeps a paper or Notes-app list today.

## MVP Feature List

1. **Smart Shopping List** -- offline-first list with automatic unit-price calculation ($/oz, $/ct, $/lb) so members can compare bulk value at a glance.
2. **Coupon Book Tracker** -- searchable, filterable digital coupon book with push alerts when a new book drops and when saved-item coupons are about to expire.
3. **Gas Price Finder** -- current warehouse gas price with community confirmations (timestamped) and a computed savings figure versus nearby non-club stations.
4. **Membership ROI Dashboard** -- logs savings (coupons, gas, unit-price wins) and projects the breakeven date when accumulated savings exceed the annual fee.
5. **Warehouse Stock Check** -- community-sourced in-stock / out-of-stock reports per warehouse with optional restock alerts on watched items.
6. **Price History** -- sparkline price-history charts per product so members know whether today's price is genuinely low.
7. **Household Sharing** -- up to 5 members per household with real-time list and savings sync.
8. **AI List Optimizer** -- paste a meal plan; get a structured Costco shopping list with quantities and estimated savings.
9. **Order Tracker** -- connect Costco.com via OAuth to surface order/delivery status inside BulkWise.

## v2 Ladder

- Sam's Club + BJ's Wholesale Club support (multi-club).
- Receipt Scanner (OCR receipts to auto-log savings and prices).
- Price Alert (notify when a watched product drops below a threshold).
- Run Assistant (aisle-ordered list + estimated trip time).
- Return Tracker (track return windows and receipts).

## v3 Ladder

- BulkWise Score (a personalized "are you getting your money's worth" score).
- Household Budget Mode (shared monthly budget with category caps).
- Corporate Accounts (team/business memberships with multi-warehouse rollups).

## Non-Goals

- **No payments / no product sales** -- BulkWise never processes checkout or sells goods.
- **No ToS-violating scraping** -- data comes from community contributions, official OAuth, and licensed/public sources; we never scrape in violation of any retailer's terms.

## Success Metrics

| Metric | Day 30 | Day 90 | Day 180 |
|---|---|---|---|
| Downloads | 5,000 | 25,000 | 75,000 |
| Weekly Active Members (WAM) | 1,500 | 8,000 | 25,000 |
| D7 Retention | 35% | 38% | 42% |
| Premium Conversion | -- | 4% | 7% |

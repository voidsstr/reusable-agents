# BulkWise -- API Specification

Base URL: `/api`. Auth via `Authorization: Bearer <jwt>`. `[auth]` = required, `[opt]` = optional (personalizes response if present). All responses are JSON. Errors use `{ "error": { "code": string, "message": string } }`.

## Auth

### POST /api/auth/google

Auth: none. Exchange a Google ID token for a BulkWise session.

```json
// request
{ "idToken": "<google-id-token>" }

// response 200
{
  "token": "<jwt>",
  "refreshToken": "<refresh-jwt>",
  "user": {
    "id": "u_01",
    "email": "member@example.com",
    "name": "Jordan Lee",
    "avatarUrl": "https://lh3.googleusercontent.com/photo.jpg",
    "householdId": "h_01",
    "membershipNumber": "111222333",
    "isPremium": false,
    "createdAt": "2026-06-01T12:00:00.000Z"
  }
}
```

### GET /api/auth/me  [auth]

Returns the authenticated user.

```json
// response 200
{
  "id": "u_01",
  "email": "member@example.com",
  "name": "Jordan Lee",
  "avatarUrl": "https://lh3.googleusercontent.com/photo.jpg",
  "householdId": "h_01",
  "homeWarehouseId": "w_01",
  "membershipNumber": "111222333",
  "isPremium": false,
  "createdAt": "2026-06-01T12:00:00.000Z"
}
```

### POST /api/auth/refresh  [auth]

Exchange a refresh token for a new access token.

```json
// request
{ "refreshToken": "<refresh-jwt>" }

// response 200
{ "token": "<jwt>", "refreshToken": "<new-refresh-jwt>", "expiresIn": 3600 }
```

## Warehouses and Gas

### GET /api/warehouses  [opt]

Query params: `lat` (float), `lng` (float) -- sort by distance when provided; `limit` (int, default 20).

```json
// response 200
{
  "warehouses": [
    {
      "id": "w_01",
      "name": "Costco Issaquah",
      "address": "1801 10th Ave NW, Issaquah, WA 98027",
      "lat": 47.535,
      "lng": -122.034,
      "distanceMiles": 2.4,
      "hasGas": true,
      "currentGasPrice": 3.59
    }
  ]
}
```

### GET /api/warehouses/:id/gas  [opt]

```json
// response 200
{
  "warehouseId": "w_01",
  "price": 3.59,
  "grade": "regular",
  "confirmedAt": "2026-06-26T15:42:00.000Z",
  "confirmCount": 7,
  "savingsVsNearby": 0.34,
  "nearbyAverage": 3.93
}
```

### POST /api/warehouses/:id/gas/confirm  [auth]

```json
// request
{ "price": 3.57, "grade": "regular" }

// response 201
{
  "id": "gc_01",
  "warehouseId": "w_01",
  "price": 3.57,
  "grade": "regular",
  "confirmedAt": "2026-06-26T16:01:00.000Z",
  "confirmCount": 8
}
```

## Lists

### GET /api/lists  [auth]

```json
// response 200
{
  "lists": [
    {
      "id": "l_01",
      "name": "Weekly Run",
      "householdId": "h_01",
      "itemCount": 12,
      "checkedCount": 3,
      "updatedAt": "2026-06-26T14:00:00.000Z"
    }
  ]
}
```

### POST /api/lists  [auth]

```json
// request
{ "name": "Weekly Run" }

// response 201
{
  "id": "l_02",
  "name": "Weekly Run",
  "householdId": "h_01",
  "itemCount": 0,
  "checkedCount": 0,
  "updatedAt": "2026-06-26T16:05:00.000Z"
}
```

### GET /api/lists/:id  [auth]

```json
// response 200
{
  "id": "l_01",
  "name": "Weekly Run",
  "householdId": "h_01",
  "updatedAt": "2026-06-26T14:00:00.000Z",
  "items": [
    {
      "id": "li_01",
      "productId": "p_01",
      "name": "Kirkland Olive Oil 2L",
      "thumbnailUrl": "https://cdn.bulkwise.app/products/p_01.jpg",
      "quantity": 1,
      "unitPrice": 0.21,
      "unitLabel": "$/oz",
      "price": 17.99,
      "couponId": "d_01",
      "couponSavings": 4.5,
      "checked": false,
      "updatedAt": "2026-06-26T14:00:00.000Z"
    }
  ]
}
```

### POST /api/lists/:id/items  [auth]

```json
// request
{ "productId": "p_01", "quantity": 1 }

// response 201
{
  "id": "li_02",
  "productId": "p_01",
  "name": "Kirkland Olive Oil 2L",
  "thumbnailUrl": "https://cdn.bulkwise.app/products/p_01.jpg",
  "quantity": 1,
  "unitPrice": 0.21,
  "unitLabel": "$/oz",
  "price": 17.99,
  "couponId": null,
  "couponSavings": 0,
  "checked": false,
  "updatedAt": "2026-06-26T16:10:00.000Z"
}
```

### PATCH /api/lists/:id/items/:itemId  [auth]

```json
// request (any subset of fields)
{ "quantity": 2, "checked": true }

// response 200
{ "id": "li_02", "quantity": 2, "checked": true, "updatedAt": "2026-06-26T16:12:00.000Z" }
```

### DELETE /api/lists/:id/items/:itemId  [auth]

```json
// response 200
{ "id": "li_02", "deleted": true }
```

### DELETE /api/lists/:id  [auth]

```json
// response 200
{ "id": "l_02", "deleted": true }
```

## Deals

### GET /api/deals  [opt]

Query params: `category` (string), `warehouseId` (string), `page` (int, default 1), `pageSize` (int, default 20).

```json
// response 200
{
  "page": 1,
  "pageSize": 20,
  "total": 134,
  "deals": [
    {
      "id": "d_01",
      "productId": "p_01",
      "title": "Kirkland Olive Oil 2L",
      "imageUrl": "https://cdn.bulkwise.app/products/p_01.jpg",
      "category": "pantry",
      "salePrice": 13.49,
      "originalPrice": 17.99,
      "savings": 4.5,
      "unitPrice": 0.16,
      "unitLabel": "$/oz",
      "startsAt": "2026-06-20T00:00:00.000Z",
      "expiresAt": "2026-07-12T00:00:00.000Z",
      "confirmCount": 12
    }
  ]
}
```

### GET /api/deals/:id  [opt]

```json
// response 200
{
  "id": "d_01",
  "productId": "p_01",
  "title": "Kirkland Olive Oil 2L",
  "imageUrl": "https://cdn.bulkwise.app/products/p_01.jpg",
  "category": "pantry",
  "salePrice": 13.49,
  "originalPrice": 17.99,
  "savings": 4.5,
  "unitPrice": 0.16,
  "unitLabel": "$/oz",
  "startsAt": "2026-06-20T00:00:00.000Z",
  "expiresAt": "2026-07-12T00:00:00.000Z",
  "confirmCount": 12,
  "priceHistory": [
    { "date": "2026-04-01", "price": 17.99 },
    { "date": "2026-05-01", "price": 16.99 },
    { "date": "2026-06-20", "price": 13.49 }
  ]
}
```

### POST /api/deals/:id/confirm  [auth]

```json
// request
{ "warehouseId": "w_01" }

// response 201
{
  "id": "dc_01",
  "dealId": "d_01",
  "warehouseId": "w_01",
  "confirmedAt": "2026-06-26T16:20:00.000Z",
  "confirmCount": 13
}
```

## Products and Stock

### GET /api/products/search  [opt]

Query params: `q` (string), `category` (string), `limit` (int, default 20).

```json
// response 200
{
  "products": [
    {
      "id": "p_01",
      "name": "Kirkland Olive Oil 2L",
      "upc": "096619123456",
      "category": "pantry",
      "imageUrl": "https://cdn.bulkwise.app/products/p_01.jpg",
      "currentPrice": 17.99,
      "unitPrice": 0.21,
      "unitLabel": "$/oz"
    }
  ]
}
```

### GET /api/products/barcode/:upc  [opt]

```json
// response 200
{
  "id": "p_01",
  "name": "Kirkland Olive Oil 2L",
  "upc": "096619123456",
  "category": "pantry",
  "imageUrl": "https://cdn.bulkwise.app/products/p_01.jpg",
  "currentPrice": 17.99,
  "unitPrice": 0.21,
  "unitLabel": "$/oz",
  "sizeValue": 67.6,
  "sizeUnit": "oz"
}
```

### GET /api/products/:id/stock  [opt]

Query params: `warehouseId` (string, required).

```json
// response 200
{
  "productId": "p_01",
  "warehouseId": "w_01",
  "status": "in_stock",
  "reportedAt": "2026-06-26T13:30:00.000Z",
  "reportCount": 5
}
```

### POST /api/products/:id/stock  [auth]

```json
// request
{ "warehouseId": "w_01", "status": "out_of_stock" }

// response 201
{
  "id": "sr_01",
  "productId": "p_01",
  "warehouseId": "w_01",
  "status": "out_of_stock",
  "reportedAt": "2026-06-26T16:25:00.000Z",
  "reportCount": 6
}
```

## Savings

### GET /api/savings  [auth]

```json
// response 200
{
  "totalSaved": 214.5,
  "annualFee": 120,
  "breakevenDate": "2026-05-09",
  "breakevenProgress": 1.0,
  "byCategory": {
    "coupons": 120.0,
    "gas": 54.5,
    "unitPrice": 40.0
  },
  "recent": [
    {
      "id": "sl_01",
      "source": "coupon",
      "amount": 4.5,
      "note": "Kirkland Olive Oil coupon",
      "loggedAt": "2026-06-26T14:00:00.000Z"
    }
  ]
}
```

### POST /api/savings/log  [auth]

```json
// request
{ "source": "gas", "amount": 3.4, "note": "14 gal @ $0.34 off" }

// response 201
{
  "id": "sl_02",
  "source": "gas",
  "amount": 3.4,
  "note": "14 gal @ $0.34 off",
  "loggedAt": "2026-06-26T16:30:00.000Z",
  "totalSaved": 217.9
}
```

## Household

### POST /api/household  [auth]

Create a household; caller becomes owner.

```json
// request
{ "name": "The Lee House" }

// response 201
{
  "id": "h_01",
  "name": "The Lee House",
  "ownerId": "u_01",
  "inviteCode": "BULK-7Q2X",
  "memberCount": 1
}
```

### POST /api/household/join  [auth]

```json
// request
{ "inviteCode": "BULK-7Q2X" }

// response 200
{ "id": "h_01", "name": "The Lee House", "memberCount": 2 }
```

### GET /api/household  [auth]

```json
// response 200
{
  "id": "h_01",
  "name": "The Lee House",
  "ownerId": "u_01",
  "inviteCode": "BULK-7Q2X",
  "members": [
    { "id": "u_01", "name": "Jordan Lee", "avatarUrl": "https://lh3.googleusercontent.com/photo1.jpg", "role": "owner" },
    { "id": "u_02", "name": "Sam Lee", "avatarUrl": "https://lh3.googleusercontent.com/photo2.jpg", "role": "member" }
  ]
}
```

## AI

### POST /api/ai/optimize-list  [auth]

Rate limit: 10 req/min/user. Converts meal-plan text into Costco product suggestions with savings estimates.

```json
// request
{
  "mealPlan": "Taco night for 6, spaghetti twice, breakfast smoothies all week",
  "warehouseId": "w_01"
}

// response 200
{
  "suggestions": [
    {
      "productId": "p_22",
      "name": "Kirkland Ground Beef 5lb",
      "quantity": 1,
      "reason": "Taco night for 6 + spaghetti x2 -- 5lb covers both meals",
      "estimatedPrice": 24.99,
      "estimatedSavings": 6.0,
      "unitPrice": 5.0,
      "unitLabel": "$/lb"
    },
    {
      "productId": "p_45",
      "name": "Kirkland Frozen Strawberries 4lb",
      "quantity": 1,
      "reason": "Breakfast smoothies all week",
      "estimatedPrice": 9.99,
      "estimatedSavings": 3.0,
      "unitPrice": 2.5,
      "unitLabel": "$/lb"
    }
  ],
  "totalEstimatedPrice": 142.4,
  "totalEstimatedSavings": 28.5,
  "model": "gpt-4o-mini"
}
```

## WebSocket

### WS /ws

Real-time list sync and gas price updates. Connect with query param `?token=<jwt>`.

**Client to server messages:**

```json
{ "type": "subscribe_list", "listId": "l_01" }
{ "type": "subscribe_warehouse", "warehouseId": "w_01" }
```

**Server to client messages:**

```json
{ "type": "list_item_updated", "listId": "l_01", "item": { "id": "li_01", "checked": true, "quantity": 2, "updatedAt": "2026-06-26T16:40:00.000Z" } }
{ "type": "list_item_added", "listId": "l_01", "item": { "id": "li_03", "productId": "p_05", "name": "Bananas 3lb", "thumbnailUrl": "https://cdn.bulkwise.app/products/p_05.jpg", "quantity": 1, "unitPrice": 0.59, "unitLabel": "$/lb", "price": 1.79, "couponId": null, "couponSavings": 0, "checked": false, "updatedAt": "2026-06-26T16:41:00.000Z" } }
{ "type": "list_item_deleted", "listId": "l_01", "itemId": "li_03" }
{ "type": "gas_price_updated", "warehouseId": "w_01", "price": 3.57, "grade": "regular", "confirmedAt": "2026-06-26T16:42:00.000Z", "confirmCount": 9, "savingsVsNearby": 0.36, "nearbyAverage": 3.93 }
```

# m

Paid, stateless network relay.

## Flow

1. `POST /api/checkout` creates a Stripe subscription checkout.
2. Stripe returns to `GET /api/access?session_id=...` and issues a signed access token.
3. `POST /api/network` verifies the token and live subscription, then relays an opaque JSON event to an allowlisted HTTPS origin.
4. The destination receives `X-M-Id` and `X-M-Signature` and the caller receives the same signed receipt.

No account database, content store, dashboard, analytics layer, or application-specific schema is included.

## Vercel environment

- `STRIPE_SECRET_KEY`
- `STRIPE_PRICE_ID`
- `ACCESS_TOKEN_SECRET`
- `NETWORK_ALLOWLIST` — comma-separated HTTPS origins
- `NETWORK_SIGNING_SECRET` — optional; falls back to `ACCESS_TOKEN_SECRET`
- `PUBLIC_ORIGIN` — optional canonical deployment origin

## Request

```bash
curl -X POST https://YOUR_DOMAIN/api/network \
  -H "Authorization: Bearer ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"destination":"https://allowed.example/hook","event":{"type":"example","value":1}}'
```

## Test

```bash
npm test
```

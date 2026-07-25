# M — Transparent Closure Architecture

M is a verifiable integration layer for payments, platform actions, and internal contracts.

Every accepted event preserves:

1. source identity and raw payload digest;
2. signature-verification result;
3. declared adapter ID, version, and rules digest;
4. normalized observed facts;
5. separately declared inferred relations;
6. contract evidence or state transition;
7. previous and resulting closure digests.

## Vercel environment variables

- `UPSTASH_REDIS_REST_URL`
- `UPSTASH_REDIS_REST_TOKEN`
- `STRIPE_WEBHOOK_SECRET`
- `TAGTOKN_CONNECTOR_SECRET`

The system fails closed: signed events are rejected if append-only storage is unavailable.

## Endpoints

- `GET /api/health`
- `POST /api/webhooks/stripe`
- `POST /api/closure/observe`
- `POST /api/contracts/transition`
- `GET /api/closure/ledger`
- `POST /api/closure/verify`

## Local verification

```bash
npm test
```

No external runtime dependencies are required.

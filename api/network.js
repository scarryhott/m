import {
  activeSubscription,
  allowedDestination,
  authorizationToken,
  json,
  networkSignature,
  readJson,
  sha256,
  stripeRequest,
  verifyAccessToken,
} from '../lib/core.js'

export function GET() {
  return json({
    service: 'm',
    protocol: 1,
    authorization: 'Bearer token',
    input: { destination: 'https://allowed-origin/path', event: 'any JSON value' },
    output: { id: 'sha256', delivered: true, status: 200, digest: 'sha256', signature: 'hmac-sha256' },
  })
}

export async function POST(request) {
  try {
    const auth = verifyAccessToken(authorizationToken(request))
    if (!auth.valid) return json({ error: 'Access token is invalid.', code: auth.reason }, 401)

    const subscription = await stripeRequest(`/subscriptions/${encodeURIComponent(auth.payload.subscription)}`)
    if (!activeSubscription(subscription)) {
      return json({ error: 'Subscription is inactive.', code: 'subscription_inactive' }, 403)
    }

    const { value } = await readJson(request)
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      return json({ error: 'Request must be an object.', code: 'invalid_request' }, 400)
    }
    if (!Object.hasOwn(value, 'event')) {
      return json({ error: 'event is required.', code: 'event_required' }, 400)
    }

    const destinationCheck = allowedDestination(value.destination)
    if (!destinationCheck.allowed) {
      return json({ error: 'Destination is not allowed.', code: destinationCheck.reason }, 403)
    }

    const at = new Date().toISOString()
    const eventDigest = sha256(JSON.stringify(value.event))
    const id = sha256(`${auth.payload.subscription}:${at}:${eventDigest}`)
    const envelope = {
      network: 'm',
      protocol: 1,
      id,
      at,
      event: value.event,
    }
    const signature = networkSignature(envelope)

    const delivered = await fetch(destinationCheck.destination, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-M-Id': id,
        'X-M-Signature': `sha256=${signature}`,
      },
      body: JSON.stringify(envelope),
      redirect: 'error',
    })

    if (!delivered.ok) {
      return json({ id, delivered: false, status: delivered.status, digest: eventDigest, signature }, 502)
    }

    return json({ id, delivered: true, status: delivered.status, digest: eventDigest, signature }, 202)
  } catch (error) {
    return json({ error: error.message, code: error.code || 'network_error' }, error.status || 500)
  }
}

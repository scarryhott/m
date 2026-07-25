import { createHash, createHmac, timingSafeEqual } from 'node:crypto'

const TOKEN_VERSION = 1
const TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30
const MAX_BODY_BYTES = 64 * 1024

export function json(payload, status = 200, headers = {}) {
  return Response.json(payload, {
    status,
    headers: {
      'Cache-Control': 'no-store',
      ...headers,
    },
  })
}

export function sha256(value) {
  return createHash('sha256').update(value).digest('hex')
}

export function hmac(secret, value) {
  return createHmac('sha256', secret).update(value).digest('hex')
}

function safeEqual(left, right) {
  const a = Buffer.from(String(left))
  const b = Buffer.from(String(right))
  return a.length === b.length && timingSafeEqual(a, b)
}

function base64url(value) {
  return Buffer.from(value).toString('base64url')
}

function decodeBase64url(value) {
  return Buffer.from(value, 'base64url').toString('utf8')
}

export function createAccessToken({ customer, subscription, now = Math.floor(Date.now() / 1000) }) {
  const secret = process.env.ACCESS_TOKEN_SECRET
  if (!secret) throw configurationError('ACCESS_TOKEN_SECRET')

  const payload = {
    v: TOKEN_VERSION,
    sub: String(customer),
    subscription: String(subscription),
    iat: now,
    exp: now + TOKEN_TTL_SECONDS,
  }
  const encoded = base64url(JSON.stringify(payload))
  return `${encoded}.${hmac(secret, encoded)}`
}

export function verifyAccessToken(token, now = Math.floor(Date.now() / 1000)) {
  const secret = process.env.ACCESS_TOKEN_SECRET
  if (!secret) throw configurationError('ACCESS_TOKEN_SECRET')

  const [encoded, signature, extra] = String(token || '').split('.')
  if (!encoded || !signature || extra) return { valid: false, reason: 'malformed' }
  if (!safeEqual(hmac(secret, encoded), signature)) return { valid: false, reason: 'signature' }

  try {
    const payload = JSON.parse(decodeBase64url(encoded))
    if (payload.v !== TOKEN_VERSION) return { valid: false, reason: 'version' }
    if (!payload.sub || !payload.subscription) return { valid: false, reason: 'claims' }
    if (!Number.isFinite(payload.exp) || payload.exp <= now) return { valid: false, reason: 'expired' }
    return { valid: true, payload }
  } catch {
    return { valid: false, reason: 'payload' }
  }
}

export function requestOrigin(request) {
  const configured = process.env.PUBLIC_ORIGIN?.trim()
  if (configured) return configured.replace(/\/$/, '')

  const forwardedHost = request.headers.get('x-forwarded-host')
  const host = forwardedHost || request.headers.get('host')
  if (!host) throw new Error('Request host is unavailable.')
  const protocol = request.headers.get('x-forwarded-proto') || 'https'
  return `${protocol}://${host}`
}

export async function stripeRequest(path, options = {}) {
  const secret = process.env.STRIPE_SECRET_KEY
  if (!secret) throw configurationError('STRIPE_SECRET_KEY')

  const response = await fetch(`https://api.stripe.com/v1${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${secret}`,
      ...(options.body ? { 'Content-Type': 'application/x-www-form-urlencoded' } : {}),
      ...(options.headers || {}),
    },
  })

  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(payload?.error?.message || 'Stripe request failed.')
    error.status = response.status
    error.code = payload?.error?.code || 'stripe_error'
    throw error
  }
  return payload
}

export function activeSubscription(subscription) {
  return Boolean(subscription && ['active', 'trialing'].includes(subscription.status))
}

export async function readJson(request) {
  const raw = await request.text()
  if (Buffer.byteLength(raw, 'utf8') > MAX_BODY_BYTES) {
    const error = new Error('Request body exceeds 64 KiB.')
    error.status = 413
    error.code = 'body_too_large'
    throw error
  }

  try {
    return { raw, value: JSON.parse(raw) }
  } catch {
    const error = new Error('Request body must be valid JSON.')
    error.status = 400
    error.code = 'invalid_json'
    throw error
  }
}

export function allowedDestination(value) {
  let destination
  try {
    destination = new URL(String(value || ''))
  } catch {
    return { allowed: false, reason: 'invalid_url' }
  }

  if (destination.protocol !== 'https:') return { allowed: false, reason: 'https_required' }

  const allowlist = String(process.env.NETWORK_ALLOWLIST || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)

  if (!allowlist.length) return { allowed: false, reason: 'allowlist_empty' }
  if (!allowlist.includes(destination.origin)) return { allowed: false, reason: 'origin_not_allowed' }
  return { allowed: true, destination }
}

export function authorizationToken(request) {
  const header = request.headers.get('authorization') || ''
  return header.startsWith('Bearer ') ? header.slice(7).trim() : ''
}

export function networkSignature(envelope) {
  const secret = process.env.NETWORK_SIGNING_SECRET || process.env.ACCESS_TOKEN_SECRET
  if (!secret) throw configurationError('NETWORK_SIGNING_SECRET or ACCESS_TOKEN_SECRET')
  return hmac(secret, JSON.stringify(envelope))
}

export function configurationError(name) {
  const error = new Error(`${name} is not configured.`)
  error.status = 503
  error.code = 'not_configured'
  return error
}

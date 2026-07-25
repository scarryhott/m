import { createHash, createHmac, timingSafeEqual } from 'node:crypto'

export const ARCHITECTURE_SCHEMA = 'tagtokn.transparent-closure/v1'
export const LEDGER_KEY = 'tagtokn:transparent-ledger:v1'
export const HEAD_KEY = 'tagtokn:transparent-head:v1'

const CONTRACT_TRANSITIONS = {
  none: ['proposed'],
  proposed: ['accepted', 'rejected', 'revised'],
  revised: ['accepted', 'rejected', 'revised'],
  accepted: ['active', 'revised', 'disputed', 'terminated'],
  active: ['fulfilled', 'revised', 'disputed', 'terminated'],
  disputed: ['resolved', 'revised', 'terminated'],
  resolved: ['active', 'fulfilled', 'terminated'],
  fulfilled: [],
  rejected: [],
  terminated: [],
}

function cleanScalar(value, max = 500) {
  if (value === null || value === undefined) return null
  if (typeof value === 'boolean' || typeof value === 'number') return value
  return String(value).trim().slice(0, max)
}

function cleanObject(value, depth = 0) {
  if (depth > 8) return '[depth-limit]'
  if (value === null || value === undefined) return null
  if (Array.isArray(value)) return value.map((item) => cleanObject(item, depth + 1))
  if (typeof value !== 'object') return cleanScalar(value, 2000)

  return Object.keys(value)
    .sort()
    .reduce((result, key) => {
      const cleaned = cleanObject(value[key], depth + 1)
      if (cleaned !== undefined) result[key] = cleaned
      return result
    }, {})
}

export function canonicalJson(value) {
  return JSON.stringify(cleanObject(value))
}

export function sha256(value) {
  const input = typeof value === 'string' ? value : canonicalJson(value)
  return createHash('sha256').update(input, 'utf8').digest('hex')
}

export function hmacSha256(secret, value) {
  return createHmac('sha256', secret).update(value, 'utf8').digest('hex')
}

function safeEqualHex(left, right) {
  if (!/^[a-f0-9]{64}$/i.test(left || '') || !/^[a-f0-9]{64}$/i.test(right || '')) return false
  const a = Buffer.from(left, 'hex')
  const b = Buffer.from(right, 'hex')
  return a.length === b.length && timingSafeEqual(a, b)
}

export function parseStripeSignature(header = '') {
  const parts = String(header)
    .split(',')
    .map((part) => part.trim().split('='))
  const timestamp = Number(parts.find(([key]) => key === 't')?.[1])
  const signatures = parts.filter(([key]) => key === 'v1').map(([, value]) => value)
  return { timestamp, signatures }
}

export function verifyStripeSignature(rawBody, header, secret, options = {}) {
  const { toleranceSeconds = 300, nowSeconds = Math.floor(Date.now() / 1000) } = options
  const { timestamp, signatures } = parseStripeSignature(header)
  if (!Number.isFinite(timestamp) || !signatures.length) {
    return { verified: false, reason: 'malformed-signature-header', scheme: 'stripe-v1' }
  }
  if (Math.abs(nowSeconds - timestamp) > toleranceSeconds) {
    return { verified: false, reason: 'timestamp-outside-tolerance', scheme: 'stripe-v1', timestamp }
  }
  const expected = hmacSha256(secret, `${timestamp}.${rawBody}`)
  const verified = signatures.some((signature) => safeEqualHex(signature, expected))
  return {
    verified,
    reason: verified ? 'verified' : 'signature-mismatch',
    scheme: 'stripe-v1',
    timestamp,
    signedPayloadDigest: sha256(`${timestamp}.${rawBody}`),
  }
}

export function verifyConnectorSignature(rawBody, header, secret) {
  const supplied = String(header || '').replace(/^sha256=/i, '')
  const expected = hmacSha256(secret, rawBody)
  const verified = safeEqualHex(supplied, expected)
  return {
    verified,
    reason: verified ? 'verified' : 'signature-mismatch',
    scheme: 'tagtokn-hmac-sha256-v1',
    signedPayloadDigest: sha256(rawBody),
  }
}

function metadataObject(metadata = {}) {
  return Object.fromEntries(
    Object.entries(metadata || {})
      .filter(([, value]) => ['string', 'number', 'boolean'].includes(typeof value))
      .map(([key, value]) => [String(key).slice(0, 80), cleanScalar(value, 300)]),
  )
}

function participantReferences(metadata = {}) {
  return [metadata.payer_basis_id, metadata.recipient_basis_id, metadata.participant_basis_id]
    .filter(Boolean)
    .map((id) => ({ id: String(id).slice(0, 160), basis: 'provider-metadata' }))
}

export function normalizeStripeEvent(event) {
  const object = event?.data?.object || {}
  const metadata = metadataObject(object.metadata || {})
  const eventType = String(event?.type || 'unknown')
  let amountMinor = object.amount_received ?? object.amount_refunded ?? object.amount_total ?? object.amount ?? null
  let relationType = 'payment-provider-event'

  if (eventType === 'payment_intent.succeeded') relationType = 'payment-confirmed'
  if (eventType === 'payment_intent.payment_failed') relationType = 'payment-failed'
  if (eventType === 'charge.refunded') relationType = 'payment-refunded'
  if (eventType === 'charge.dispute.created') relationType = 'payment-disputed'
  if (eventType === 'checkout.session.completed') relationType = 'checkout-completed'
  if (!Number.isFinite(Number(amountMinor))) amountMinor = null

  const normalized = {
    source: {
      system: 'stripe',
      eventId: String(event?.id || ''),
      eventType,
      occurredAt: event?.created ? new Date(Number(event.created) * 1000).toISOString() : null,
      liveMode: Boolean(event?.livemode),
    },
    observed: {
      providerObjectId: cleanScalar(object.id, 180),
      providerObjectType: cleanScalar(object.object, 80),
      amountMinor: amountMinor === null ? null : Number(amountMinor),
      currency: object.currency ? String(object.currency).toUpperCase().slice(0, 8) : null,
      status: cleanScalar(object.status, 100),
      customerId: cleanScalar(object.customer, 180),
      paymentIntentId: cleanScalar(object.payment_intent || (object.object === 'payment_intent' ? object.id : null), 180),
      metadata,
    },
    inferred: {
      relationType,
      contractId: cleanScalar(metadata.contract_id, 180),
      communalContext: cleanScalar(metadata.communal_context, 300),
      participants: participantReferences(metadata),
      inferenceBasis: ['declared-adapter-rule', 'provider-metadata'],
    },
    contractEvidence: metadata.contract_id
      ? {
          contractId: String(metadata.contract_id),
          evidenceType: eventType,
          effect: 'evidence-only',
          advancesContractState: false,
        }
      : null,
  }

  if (!normalized.source.eventId) throw new Error('Stripe event id is required.')
  return normalized
}

export function normalizeConnectorEvent(body = {}) {
  const sourceSystem = cleanScalar(body.sourceSystem || body.source || body.connector, 120)
  const sourceEventId = cleanScalar(body.sourceEventId || body.eventId, 180)
  const sourceEventType = cleanScalar(body.sourceEventType || body.eventType || body.action, 180)
  if (!sourceSystem || !sourceEventId || !sourceEventType) {
    throw new Error('sourceSystem, sourceEventId, and sourceEventType are required.')
  }
  if (!body.observed || typeof body.observed !== 'object' || Array.isArray(body.observed)) {
    throw new Error('observed must be an object containing source facts.')
  }

  return {
    source: {
      system: sourceSystem,
      eventId: sourceEventId,
      eventType: sourceEventType,
      occurredAt: cleanScalar(body.occurredAt, 80),
      liveMode: body.liveMode === undefined ? null : Boolean(body.liveMode),
    },
    observed: cleanObject(body.observed),
    inferred: cleanObject(body.inferred || {}),
    contractEvidence: body.contractEvidence ? cleanObject(body.contractEvidence) : null,
  }
}

export function canTransitionContract(previousState, nextState) {
  const current = previousState || 'none'
  return Boolean(CONTRACT_TRANSITIONS[current]?.includes(nextState))
}

export function normalizeContractTransition(body = {}, previousState = undefined) {
  const contractId = cleanScalar(body.contractId, 180)
  const nextState = cleanScalar(body.nextState, 60)
  const sourceEventId = cleanScalar(body.sourceEventId || body.transitionId, 180)
  if (!contractId || !nextState || !sourceEventId) {
    throw new Error('contractId, nextState, and sourceEventId are required.')
  }
  if (previousState !== undefined && !canTransitionContract(previousState, nextState)) {
    throw new Error(`Invalid contract transition: ${previousState || 'none'} -> ${nextState}.`)
  }

  return {
    source: {
      system: 'tagtokn-internal-contract',
      eventId: sourceEventId,
      eventType: `contract.${nextState}`,
      occurredAt: cleanScalar(body.occurredAt, 80),
      liveMode: true,
    },
    observed: {
      contractId,
      previousState: previousState || null,
      nextState,
      participants: Array.isArray(body.participants) ? cleanObject(body.participants) : [],
      evidence: Array.isArray(body.evidence) ? cleanObject(body.evidence) : [],
      acknowledgements: cleanObject(body.acknowledgements || []),
    },
    inferred: {
      communalPurpose: cleanScalar(body.communalPurpose, 500),
      relation: cleanScalar(body.relation, 500),
      inferenceBasis: ['native-contract-transition'],
    },
    contractTransition: {
      contractId,
      previousState: previousState || null,
      nextState,
    },
  }
}

export function buildTransparentRecord({
  normalized,
  rawBody,
  signatureVerification,
  adapter,
  previousClosureDigest = null,
  receivedAt = new Date().toISOString(),
}) {
  if (!signatureVerification?.verified) throw new Error('Unverified source events cannot enter closure.')
  const rawPayloadDigest = sha256(rawBody)
  const normalizedDigest = sha256(normalized)
  const adapterDescriptor = {
    id: adapter.id,
    version: adapter.version,
    mappingDigest: sha256({ id: adapter.id, version: adapter.version, rules: adapter.rules || [] }),
  }
  const resultingClosureDigest = sha256({
    schema: ARCHITECTURE_SCHEMA,
    previousClosureDigest,
    rawPayloadDigest,
    normalizedDigest,
    adapter: adapterDescriptor,
    sourceEventId: normalized.source.eventId,
  })

  return {
    schema: ARCHITECTURE_SCHEMA,
    recordId: `${normalized.source.system}:${normalized.source.eventId}`,
    source: {
      ...normalized.source,
      receivedAt,
      rawPayloadDigest,
      signature: signatureVerification,
    },
    adapter: adapterDescriptor,
    observed: normalized.observed,
    inferred: normalized.inferred,
    contractEvidence: normalized.contractEvidence || null,
    contractTransition: normalized.contractTransition || null,
    chain: {
      previousClosureDigest,
      normalizedDigest,
      resultingClosureDigest,
    },
    verification: {
      sourceAuthentic: true,
      observedInferenceSeparated: true,
      deterministicAdapter: true,
      persistenceRequired: true,
    },
  }
}

export function verifyTransparentRecord(record) {
  const normalized = {
    source: {
      system: record?.source?.system,
      eventId: record?.source?.eventId,
      eventType: record?.source?.eventType,
      occurredAt: record?.source?.occurredAt,
      liveMode: record?.source?.liveMode,
    },
    observed: record?.observed,
    inferred: record?.inferred,
    contractEvidence: record?.contractEvidence || null,
    ...(record?.contractTransition ? { contractTransition: record.contractTransition } : {}),
  }
  const normalizedDigest = sha256(normalized)
  const resultingClosureDigest = sha256({
    schema: record?.schema,
    previousClosureDigest: record?.chain?.previousClosureDigest || null,
    rawPayloadDigest: record?.source?.rawPayloadDigest,
    normalizedDigest,
    adapter: record?.adapter,
    sourceEventId: record?.source?.eventId,
  })
  const checks = {
    schema: record?.schema === ARCHITECTURE_SCHEMA,
    sourceAuthentic: record?.source?.signature?.verified === true,
    normalizedDigest: normalizedDigest === record?.chain?.normalizedDigest,
    resultingClosureDigest: resultingClosureDigest === record?.chain?.resultingClosureDigest,
    observedInferenceSeparated:
      Boolean(record?.observed && typeof record.observed === 'object') &&
      Boolean(record?.inferred && typeof record.inferred === 'object'),
  }
  return { valid: Object.values(checks).every(Boolean), checks, recomputed: { normalizedDigest, resultingClosureDigest } }
}

export function storageConfiguration() {
  return {
    configured: Boolean(process.env.UPSTASH_REDIS_REST_URL && process.env.UPSTASH_REDIS_REST_TOKEN),
    urlConfigured: Boolean(process.env.UPSTASH_REDIS_REST_URL),
    tokenConfigured: Boolean(process.env.UPSTASH_REDIS_REST_TOKEN),
  }
}

async function redisCommand(command) {
  const config = storageConfiguration()
  if (!config.configured) {
    const error = new Error('Append-only storage is not configured.')
    error.status = 503
    error.code = 'storage_not_configured'
    throw error
  }
  const response = await fetch(process.env.UPSTASH_REDIS_REST_URL, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(command),
  })
  const payload = await response.json()
  if (!response.ok || payload.error) {
    const error = new Error(payload.error || `Storage request failed with ${response.status}.`)
    error.status = 502
    error.code = 'storage_request_failed'
    throw error
  }
  return payload.result
}

async function redisPipeline(commands) {
  const config = storageConfiguration()
  if (!config.configured) {
    const error = new Error('Append-only storage is not configured.')
    error.status = 503
    error.code = 'storage_not_configured'
    throw error
  }
  const base = process.env.UPSTASH_REDIS_REST_URL.replace(/\/$/, '')
  const response = await fetch(`${base}/pipeline`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(commands),
  })
  const payload = await response.json()
  if (!response.ok || payload.error || payload.some?.((entry) => entry.error)) {
    const message = payload.error || payload.find?.((entry) => entry.error)?.error || `Storage request failed with ${response.status}.`
    const error = new Error(message)
    error.status = 502
    error.code = 'storage_request_failed'
    throw error
  }
  return payload.map((entry) => entry.result)
}

function eventKey(source) {
  return `tagtokn:event:${source.system}:${source.eventId}`
}

function contractKey(contractId) {
  return contractId ? `tagtokn:contract:${contractId}:state` : 'tagtokn:contract:none'
}

const APPEND_SCRIPT = `
local existing = redis.call('GET', KEYS[1])
if existing then return {0, existing} end
local currentHead = redis.call('GET', KEYS[3]) or ''
if currentHead ~= ARGV[3] then return {-1, currentHead} end
if ARGV[5] ~= '' then
  local currentContract = redis.call('GET', KEYS[4]) or ''
  if currentContract ~= ARGV[4] then return {-2, currentContract} end
end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('LPUSH', KEYS[2], ARGV[1])
redis.call('LTRIM', KEYS[2], 0, 9999)
redis.call('SET', KEYS[3], ARGV[2])
if ARGV[5] ~= '' then redis.call('SET', KEYS[4], ARGV[5]) end
return {1, ARGV[1]}
`

export async function appendVerifiedEvent({ rawBody, signatureVerification, adapter, normalize }) {
  let parsedSource
  try {
    parsedSource = JSON.parse(rawBody)
  } catch {
    const error = new Error('Request body must be valid JSON.')
    error.status = 400
    error.code = 'invalid_json'
    throw error
  }

  const provisional = await normalize(parsedSource, undefined)
  const key = eventKey(provisional.source)
  const transitionContractId = provisional.contractTransition?.contractId || null

  for (let attempt = 0; attempt < 4; attempt += 1) {
    const commands = [['GET', HEAD_KEY], ['GET', key]]
    if (transitionContractId) commands.push(['GET', contractKey(transitionContractId)])
    const [head, existing, currentContractState = null] = await redisPipeline(commands)
    if (existing) return { duplicate: true, record: JSON.parse(existing) }

    const normalized = await normalize(parsedSource, currentContractState)
    const record = buildTransparentRecord({
      normalized,
      rawBody,
      signatureVerification,
      adapter,
      previousClosureDigest: head || null,
    })
    const recordJson = canonicalJson(record)
    const expectedContract = currentContractState || ''
    const nextContract = normalized.contractTransition?.nextState || ''
    const result = await redisCommand([
      'EVAL',
      APPEND_SCRIPT,
      4,
      key,
      LEDGER_KEY,
      HEAD_KEY,
      contractKey(transitionContractId),
      recordJson,
      record.chain.resultingClosureDigest,
      head || '',
      expectedContract,
      nextContract,
    ])

    const [status, value] = result || []
    if (Number(status) === 1) return { duplicate: false, record }
    if (Number(status) === 0) return { duplicate: true, record: JSON.parse(value) }
    if (Number(status) === -1 || Number(status) === -2) continue
    throw new Error('Unexpected storage append result.')
  }

  const error = new Error('Concurrent append conflict; retry the event.')
  error.status = 409
  error.code = 'append_conflict'
  throw error
}

export async function readLedger(limit = 25) {
  const safeLimit = Math.max(1, Math.min(Number(limit) || 25, 100))
  const [head, records] = await redisPipeline([
    ['GET', HEAD_KEY],
    ['LRANGE', LEDGER_KEY, 0, safeLimit - 1],
  ])
  return {
    head: head || null,
    records: (records || []).map((record) => JSON.parse(record)),
  }
}

export function runtimeConfiguration() {
  return {
    architecture: ARCHITECTURE_SCHEMA,
    storage: storageConfiguration(),
    integrations: {
      stripe: {
        endpoint: '/api/webhooks/stripe',
        configured: Boolean(process.env.STRIPE_WEBHOOK_SECRET),
        secretName: 'STRIPE_WEBHOOK_SECRET',
      },
      platformAdapter: {
        endpoint: '/api/closure/observe',
        configured: Boolean(process.env.TAGTOKN_CONNECTOR_SECRET),
        secretName: 'TAGTOKN_CONNECTOR_SECRET',
      },
      internalContracts: {
        endpoint: '/api/contracts/transition',
        configured: Boolean(process.env.TAGTOKN_CONNECTOR_SECRET),
        secretName: 'TAGTOKN_CONNECTOR_SECRET',
      },
      verifier: { endpoint: '/api/closure/verify', configured: true },
    },
  }
}

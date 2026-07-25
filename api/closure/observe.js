import {
  appendVerifiedEvent,
  normalizeConnectorEvent,
  verifyConnectorSignature,
  verifyTransparentRecord,
} from '../_closure.js'

function json(payload, status = 200) {
  return Response.json(payload, { status, headers: { 'Cache-Control': 'no-store' } })
}

export async function POST(request) {
  try {
    const secret = process.env.TAGTOKN_CONNECTOR_SECRET
    if (!secret) return json({ error: 'TAGTOKN_CONNECTOR_SECRET is not configured.', code: 'connector_not_configured' }, 503)

    const rawBody = await request.text()
    const signatureVerification = verifyConnectorSignature(
      rawBody,
      request.headers.get('x-tagtokn-signature') || '',
      secret,
    )
    if (!signatureVerification.verified) {
      return json({ error: 'Connector signature verification failed.', verification: signatureVerification }, 400)
    }

    const result = await appendVerifiedEvent({
      rawBody,
      signatureVerification,
      adapter: {
        id: request.headers.get('x-tagtokn-adapter-id') || 'external-platform-adapter',
        version: request.headers.get('x-tagtokn-adapter-version') || '1.0.0',
        rules: [
          'preserve source facts inside observed',
          'keep inferred relations explicitly separate',
          'retain external event identity and adapter provenance',
        ],
      },
      normalize: normalizeConnectorEvent,
    })

    return json({
      accepted: true,
      duplicate: result.duplicate,
      record: result.record,
      verification: verifyTransparentRecord(result.record),
    })
  } catch (error) {
    return json(
      { error: error.message || 'Platform event could not be integrated.', code: error.code || 'connector_integration_error' },
      error.status || 400,
    )
  }
}

export function GET() {
  return json({
    endpoint: '/api/closure/observe',
    method: 'POST',
    signatureHeader: 'X-TagTokn-Signature: sha256=<HMAC raw body>',
    requiredFields: ['sourceSystem', 'sourceEventId', 'sourceEventType', 'observed'],
    separationRule: 'observed source facts and inferred relational interpretation must be distinct objects',
  })
}

import {
  appendVerifiedEvent,
  normalizeContractTransition,
  verifyConnectorSignature,
  verifyTransparentRecord,
} from '../_closure.js'

function json(payload, status = 200) {
  return Response.json(payload, { status, headers: { 'Cache-Control': 'no-store' } })
}

export async function POST(request) {
  try {
    const secret = process.env.TAGTOKN_CONNECTOR_SECRET
    if (!secret) return json({ error: 'TAGTOKN_CONNECTOR_SECRET is not configured.', code: 'contract_not_configured' }, 503)

    const rawBody = await request.text()
    const signatureVerification = verifyConnectorSignature(
      rawBody,
      request.headers.get('x-tagtokn-signature') || '',
      secret,
    )
    if (!signatureVerification.verified) {
      return json({ error: 'Contract signature verification failed.', verification: signatureVerification }, 400)
    }

    const result = await appendVerifiedEvent({
      rawBody,
      signatureVerification,
      adapter: {
        id: 'tagtokn-native-contract-adapter',
        version: '1.0.0',
        rules: [
          'validate the proposed transition against persisted prior state',
          'preserve participants acknowledgements and evidence',
          'append contract state and closure head atomically',
        ],
      },
      normalize: normalizeContractTransition,
    })

    return json({
      accepted: true,
      duplicate: result.duplicate,
      record: result.record,
      verification: verifyTransparentRecord(result.record),
    })
  } catch (error) {
    return json(
      { error: error.message || 'Contract transition could not be integrated.', code: error.code || 'contract_integration_error' },
      error.status || 400,
    )
  }
}

export function GET() {
  return json({
    endpoint: '/api/contracts/transition',
    method: 'POST',
    signatureHeader: 'X-TagTokn-Signature: sha256=<HMAC raw body>',
    states: ['proposed', 'accepted', 'active', 'fulfilled', 'revised', 'disputed', 'resolved', 'rejected', 'terminated'],
  })
}

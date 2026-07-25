import {
  appendVerifiedEvent,
  normalizeStripeEvent,
  verifyStripeSignature,
  verifyTransparentRecord,
} from '../_closure.js'

function json(payload, status = 200) {
  return Response.json(payload, { status, headers: { 'Cache-Control': 'no-store' } })
}

export async function POST(request) {
  try {
    const secret = process.env.STRIPE_WEBHOOK_SECRET
    if (!secret) return json({ error: 'STRIPE_WEBHOOK_SECRET is not configured.', code: 'stripe_not_configured' }, 503)

    const rawBody = await request.text()
    const signatureVerification = verifyStripeSignature(
      rawBody,
      request.headers.get('stripe-signature') || '',
      secret,
    )
    if (!signatureVerification.verified) {
      return json({ error: 'Stripe signature verification failed.', verification: signatureVerification }, 400)
    }

    const result = await appendVerifiedEvent({
      rawBody,
      signatureVerification,
      adapter: {
        id: 'stripe-payment-adapter',
        version: '1.0.0',
        rules: [
          'preserve provider facts as observed',
          'derive relations only from declared rules and provider metadata',
          'record payment as contract evidence without advancing contract state',
        ],
      },
      normalize: normalizeStripeEvent,
    })

    return json({
      accepted: true,
      duplicate: result.duplicate,
      record: result.record,
      verification: verifyTransparentRecord(result.record),
    })
  } catch (error) {
    return json(
      { error: error.message || 'Stripe event could not be integrated.', code: error.code || 'stripe_integration_error' },
      error.status || 400,
    )
  }
}

export function GET() {
  return json({
    endpoint: '/api/webhooks/stripe',
    method: 'POST',
    signatureHeader: 'Stripe-Signature',
    supportedEvents: [
      'payment_intent.succeeded',
      'payment_intent.payment_failed',
      'charge.refunded',
      'charge.dispute.created',
      'checkout.session.completed',
    ],
  })
}

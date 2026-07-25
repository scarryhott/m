import { json, requestOrigin, stripeRequest } from '../lib/core.js'

export async function POST(request) {
  try {
    const price = process.env.STRIPE_PRICE_ID
    if (!price) return json({ error: 'STRIPE_PRICE_ID is not configured.', code: 'not_configured' }, 503)

    const origin = requestOrigin(request)
    const body = new URLSearchParams({
      mode: 'subscription',
      'line_items[0][price]': price,
      'line_items[0][quantity]': '1',
      success_url: `${origin}/api/access?session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${origin}/`,
    })

    const session = await stripeRequest('/checkout/sessions', {
      method: 'POST',
      body,
    })

    return new Response(null, {
      status: 303,
      headers: {
        Location: session.url,
        'Cache-Control': 'no-store',
      },
    })
  } catch (error) {
    return json({ error: error.message, code: error.code || 'checkout_error' }, error.status || 500)
  }
}

export function GET() {
  return json({ method: 'POST' }, 405, { Allow: 'POST' })
}

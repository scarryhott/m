import { activeSubscription, createAccessToken, json, stripeRequest } from '../lib/core.js'

export async function GET(request) {
  try {
    const sessionId = new URL(request.url).searchParams.get('session_id')
    if (!sessionId) return json({ error: 'session_id is required.', code: 'session_required' }, 400)

    const session = await stripeRequest(`/checkout/sessions/${encodeURIComponent(sessionId)}?expand[]=subscription`)
    const subscription = session.subscription

    if (session.status !== 'complete' || !activeSubscription(subscription)) {
      return json({ error: 'An active subscription is required.', code: 'subscription_inactive' }, 403)
    }

    const token = createAccessToken({
      customer: session.customer,
      subscription: subscription.id,
    })

    const claims = JSON.parse(Buffer.from(token.split('.')[0], 'base64url').toString('utf8'))
    return json({
      token,
      endpoint: '/api/network',
      expiresAt: new Date(claims.exp * 1000).toISOString(),
    })
  } catch (error) {
    return json({ error: error.message, code: error.code || 'access_error' }, error.status || 500)
  }
}

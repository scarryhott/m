import { verifyTransparentRecord } from '../_closure.js'

function json(payload, status = 200) {
  return Response.json(payload, { status, headers: { 'Cache-Control': 'no-store' } })
}

export async function POST(request) {
  try {
    const record = await request.json()
    const verification = verifyTransparentRecord(record)
    return json(verification, verification.valid ? 200 : 422)
  } catch (error) {
    return json({ valid: false, error: error.message || 'Record could not be verified.' }, 400)
  }
}

export function GET() {
  return json({
    endpoint: '/api/closure/verify',
    method: 'POST',
    description: 'Recomputes normalized and resulting closure digests independently from a supplied record.',
  })
}

import { readLedger, runtimeConfiguration, verifyTransparentRecord } from '../_closure.js'

function json(payload, status = 200) {
  return Response.json(payload, { status, headers: { 'Cache-Control': 'no-store' } })
}

export async function GET(request) {
  const configuration = runtimeConfiguration()
  const limit = new URL(request.url).searchParams.get('limit') || 25

  if (!configuration.storage.configured) {
    return json({
      configuration,
      head: null,
      records: [],
      verified: false,
      reason: 'Append-only storage is not configured. Events are rejected rather than accepted without persistence.',
    })
  }

  try {
    const ledger = await readLedger(limit)
    return json({
      configuration,
      ...ledger,
      records: ledger.records.map((record) => ({
        ...record,
        independentVerification: verifyTransparentRecord(record),
      })),
    })
  } catch (error) {
    return json({ configuration, error: error.message, code: error.code || 'ledger_error' }, error.status || 500)
  }
}

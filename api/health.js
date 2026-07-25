import { runtimeConfiguration } from './_closure.js'

export function GET() {
  return Response.json({
    ok: true,
    service: 'm-transparent-closure',
    configuration: runtimeConfiguration(),
  }, { headers: { 'Cache-Control': 'no-store' } })
}

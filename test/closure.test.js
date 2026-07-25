import test from 'node:test'
import assert from 'node:assert/strict'
import {
  buildTransparentRecord,
  canTransitionContract,
  hmacSha256,
  normalizeConnectorEvent,
  normalizeContractTransition,
  normalizeStripeEvent,
  verifyConnectorSignature,
  verifyStripeSignature,
  verifyTransparentRecord,
} from '../api/_closure.js'

test('Stripe signature verifies exact raw body and rejects mutation', () => {
  const raw = '{"id":"evt_1"}'
  const secret = 'whsec_test'
  const timestamp = 1_700_000_000
  const signature = hmacSha256(secret, `${timestamp}.${raw}`)
  const header = `t=${timestamp},v1=${signature}`
  assert.equal(verifyStripeSignature(raw, header, secret, { nowSeconds: timestamp }).verified, true)
  assert.equal(verifyStripeSignature(`${raw} `, header, secret, { nowSeconds: timestamp }).verified, false)
})

test('Connector signature verifies exact raw body', () => {
  const raw = '{"sourceSystem":"github"}'
  const secret = 'connector-secret'
  const header = `sha256=${hmacSha256(secret, raw)}`
  assert.equal(verifyConnectorSignature(raw, header, secret).verified, true)
  assert.equal(verifyConnectorSignature(raw + 'x', header, secret).verified, false)
})

test('Stripe normalization keeps observed and inferred data separate', () => {
  const normalized = normalizeStripeEvent({
    id: 'evt_1', type: 'payment_intent.succeeded', created: 1_700_000_000,
    data: { object: { id: 'pi_1', object: 'payment_intent', amount_received: 2500, currency: 'usd', status: 'succeeded', metadata: { contract_id: 'contract-1' } } },
  })
  assert.equal(normalized.observed.amountMinor, 2500)
  assert.equal(normalized.inferred.contractId, 'contract-1')
  assert.equal(normalized.contractEvidence.advancesContractState, false)
})

test('Platform normalization requires source identity and observed facts', () => {
  assert.throws(() => normalizeConnectorEvent({ sourceSystem: 'github' }))
  const normalized = normalizeConnectorEvent({ sourceSystem: 'github', sourceEventId: 'delivery-1', sourceEventType: 'pull_request.merged', observed: { merged: true }, inferred: { relationType: 'collaboration-continued' } })
  assert.equal(normalized.observed.merged, true)
  assert.equal(normalized.inferred.relationType, 'collaboration-continued')
})

test('Contract transitions are state checked', () => {
  assert.equal(canTransitionContract(null, 'proposed'), true)
  assert.equal(canTransitionContract('proposed', 'accepted'), true)
  assert.equal(canTransitionContract('proposed', 'fulfilled'), false)
  assert.throws(() => normalizeContractTransition({ contractId: 'c1', sourceEventId: 't1', nextState: 'fulfilled' }, 'proposed'))
})

test('Transparent record is deterministic and independently verifiable', () => {
  const normalized = normalizeConnectorEvent({ sourceSystem: 'github', sourceEventId: 'delivery-1', sourceEventType: 'pull_request.merged', observed: { merged: true }, inferred: { relationType: 'collaboration-continued' } })
  const record = buildTransparentRecord({ normalized, rawBody: JSON.stringify(normalized), signatureVerification: { verified: true, scheme: 'test' }, adapter: { id: 'github', version: '1', rules: ['separate facts and inference'] }, previousClosureDigest: null, receivedAt: '2026-07-25T00:00:00.000Z' })
  assert.equal(verifyTransparentRecord(record).valid, true)
  const tampered = structuredClone(record)
  tampered.observed.merged = false
  assert.equal(verifyTransparentRecord(tampered).valid, false)
})

test('Contract transition normalizes append-only evidence', () => {
  const n = normalizeContractTransition({ contractId: 'c1', sourceEventId: 't1', nextState: 'accepted', participants: [{ id: 'a' }, { id: 'b' }], evidence: ['proposal-digest'] }, 'proposed')
  assert.equal(n.contractTransition.previousState, 'proposed')
  assert.equal(n.contractTransition.nextState, 'accepted')
  assert.equal(n.observed.evidence[0], 'proposal-digest')
})

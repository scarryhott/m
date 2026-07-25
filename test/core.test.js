import test from 'node:test'
import assert from 'node:assert/strict'
import { allowedDestination, createAccessToken, verifyAccessToken } from '../lib/core.js'

test('access token verifies and expires', () => {
  process.env.ACCESS_TOKEN_SECRET = 'test-secret'
  const token = createAccessToken({ customer: 'cus_1', subscription: 'sub_1', now: 100 })
  assert.equal(verifyAccessToken(token, 101).valid, true)
  assert.equal(verifyAccessToken(token, 100 + 60 * 60 * 24 * 31).valid, false)
})

test('access token rejects mutation', () => {
  process.env.ACCESS_TOKEN_SECRET = 'test-secret'
  const token = createAccessToken({ customer: 'cus_1', subscription: 'sub_1', now: 100 })
  assert.equal(verifyAccessToken(`${token}x`, 101).valid, false)
})

test('network destination is exact-origin allowlisted', () => {
  process.env.NETWORK_ALLOWLIST = 'https://example.com,https://api.example.net'
  assert.equal(allowedDestination('https://example.com/hook').allowed, true)
  assert.equal(allowedDestination('http://example.com/hook').allowed, false)
  assert.equal(allowedDestination('https://evil.example.com/hook').allowed, false)
})

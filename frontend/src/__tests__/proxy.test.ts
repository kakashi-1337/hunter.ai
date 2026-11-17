import { describe, expect, it } from 'vitest'
import { proxyAddress, proxyInEffect, proxyTestText } from '../proxy'
import type { ProxyTest } from '../types'

const result = (over: Partial<ProxyTest> = {}): ProxyTest =>
  ({ ok: true, through: 'fr.example.net:1080', ms: 420, ip: '185.1.2.3', country: 'FR', error: '', ...over })

describe('the address of a proxy as it fits in a badge', () => {
  it('is the host and the port, without the kind and without the user name', () => {
    expect(proxyAddress('socks5://hunter@fr.example.net:1080')).toBe('fr.example.net:1080')
    expect(proxyAddress('http://127.0.0.1:8080')).toBe('127.0.0.1:8080')
    expect(proxyAddress('http://me%40work@[::1]:9090')).toBe('[::1]:9090')
    expect(proxyAddress('')).toBe('')
  })
})

describe('the proxy a chat goes through', () => {
  const settings = { upstream_proxy: 'http://127.0.0.1:8080' }
  it("is the target's own when it has one", () => {
    expect(proxyInEffect({ proxy: 'socks5://hunter@fr.example.net:1080' }, settings)).toEqual({ address: 'fr.example.net:1080', own: true })
  })
  it('is the one in Settings for a target without one, and for a chat with no target', () => {
    expect(proxyInEffect({ proxy: '' }, settings)).toEqual({ address: '127.0.0.1:8080', own: false })
    expect(proxyInEffect(undefined, settings)).toEqual({ address: '127.0.0.1:8080', own: false })
  })
  it('is none when neither is set, or while the settings are still loading', () => {
    expect(proxyInEffect({ proxy: '' }, { upstream_proxy: '' })).toBeNull()
    expect(proxyInEffect(undefined, null)).toBeNull()
  })
})

describe('what the test of a proxy found', () => {
  it('names the address sites see and the country', () => {
    expect(proxyTestText(result())).toBe('Works: sites see 185.1.2.3, France (FR). Answered in 420 ms.')
    expect(proxyTestText(result({ country: '' }))).toBe('Works: sites see 185.1.2.3. Answered in 420 ms.')
    expect(proxyTestText(result({ ip: '', country: 'DE' }))).toBe('Works: sites see Germany (DE). Answered in 420 ms.')
  })
  it('says so when the proxy works and the test page could not say where the request came from', () => {
    const r = result({ ip: '', country: '', error: 'the proxy carried the request, but probe.test did not say where it came from (HTTP 200)' })
    expect(proxyTestText(r)).toBe('It carries requests (420 ms), but the test page did not say where they come from.')
  })
  it('gives the reason when it does not work', () => {
    expect(proxyTestText(result({ ok: false, ip: '', country: '', error: 'the proxy refused the user name or the password' })))
      .toBe('Not working: the proxy refused the user name or the password.')
    expect(proxyTestText(result({ ok: false, error: "nothing answers at the proxy's address (ConnectionRefusedError)." })))
      .toBe("Not working: nothing answers at the proxy's address (ConnectionRefusedError).")
  })
})

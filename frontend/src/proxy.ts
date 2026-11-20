import type { ProxyTest, Settings, Target } from './types'

/** Host and port of a proxy address as the server shows it ("socks5://user@host:1080"): what fits in a badge. */
export function proxyAddress(shown: string): string {
  return shown.replace(/^[a-z][a-z0-9+.-]*:\/\//i, '').replace(/^[^@/]*@/, '')
}

/** The proxy the traffic of a chat goes through: its target's own, else the one in Settings; null for none. */
export function proxyInEffect(
  target: Pick<Target, 'proxy'> | null | undefined, settings: Pick<Settings, 'upstream_proxy'> | null,
): { address: string; own: boolean } | null {
  if (target?.proxy) return { address: proxyAddress(target.proxy), own: true }
  if (settings?.upstream_proxy) return { address: proxyAddress(settings.upstream_proxy), own: false }
  return null
}

function countryName(code: string): string {
  try {
    return new Intl.DisplayNames(['en'], { type: 'region' }).of(code) ?? code
  } catch {
    return code
  }
}

/** What the test of a proxy found, in one line. */
export function proxyTestText(r: ProxyTest): string {
  if (!r.ok) return `Not working: ${r.error.replace(/\.$/, '')}.`
  const where = [r.ip, r.country ? `${countryName(r.country)} (${r.country})` : ''].filter(Boolean).join(', ')
  if (!where) return `It carries requests (${r.ms} ms), but the test page did not say where they come from.`
  return `Works: sites see ${where}. Answered in ${r.ms} ms.`
}

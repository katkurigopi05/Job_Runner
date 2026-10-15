/**
 * Whether a request names this machine, the dashboard's half of the rule in
 * `apps/api/middleware.py`.
 *
 * DNS rebinding points a domain an attacker owns at 127.0.0.1, so a page on
 * it reaches this server from the owner's own browser and reads the replies as
 * same-origin. The browser will not let it change the `Host` it sends, and
 * that names the attacker's domain. The dashboard is reached by `localhost` or
 * an IP address (a phone on the LAN uses the address), never by a domain name.
 *
 * No imports, and no syntax Node cannot strip: `tests/test_rebinding_guard.py`
 * runs this file with `node` rather than restating it.
 */

function hostnameOf(host: string): string | null {
  try {
    return new URL(`http://${host}`).hostname;
  } catch {
    return null;
  }
}

function isAddress(name: string): boolean {
  // The URL parser has already normalised an address: IPv4 to four decimal
  // parts, IPv6 to its bracketed form.
  return /^\d{1,3}(\.\d{1,3}){3}$/.test(name) || (name.startsWith("[") && name.endsWith("]"));
}

/** A missing `Host` is not a browser's, which always sends one. */
export function isLocalName(host: string | null): boolean {
  if (!host) return true;
  const name = hostnameOf(host);
  if (name === null) return false;
  return name === "localhost" || isAddress(name);
}

/** `null`, sent by a sandboxed or file page, names nothing and is refused. */
export function isLocalOrigin(origin: string): boolean {
  try {
    return isLocalName(new URL(origin).host);
  } catch {
    return false;
  }
}

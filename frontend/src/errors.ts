/** Turns transport and HTTP failures into messages a person can act on. Never echoes secrets. */

const SECRET_PATTERNS: RegExp[] = [
  /\bgithub_pat_[A-Za-z0-9_]{10,}/g,
  /\b(?:ghp|gho|ghu|ghs|ghr|glpat)[-_][A-Za-z0-9_-]{10,}/g,
  /\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}/gi,
  /\b(api[_-]?key|token|password|secret|authorization)(["']?\s*[:=]\s*["']?)[^\s"',;]{6,}/gi,
  /(https?:\/\/)[^\s/@:]+:[^\s/@]+@/gi,
];

export function redactSecrets(text: string): string {
  let out = text;
  out = out.replace(SECRET_PATTERNS[0], "[redacted]");
  out = out.replace(SECRET_PATTERNS[1], "[redacted]");
  out = out.replace(SECRET_PATTERNS[2], "$1 [redacted]");
  out = out.replace(SECRET_PATTERNS[3], "$1$2[redacted]");
  out = out.replace(SECRET_PATTERNS[4], "$1[redacted]@");
  return out;
}

/** True for a value that looks like a personal access token rather than a repository URL. */
export function looksLikeToken(value: string): boolean {
  const v = value.trim();
  if (v === "") return false;
  if (/^(?:github_pat_|ghp_|gho_|ghu_|ghs_|ghr_|glpat-)[A-Za-z0-9_-]{8,}/.test(v))
    return true;
  // An Azure DevOps style token: a long run of base64 characters with no URL or path shape.
  return /^[A-Za-z0-9]{40,}$/.test(v);
}

export function isNetworkFailure(e: unknown): boolean {
  if (!(e instanceof Error)) return false;
  return (
    e instanceof TypeError ||
    /failed to fetch|networkerror|load failed|network request failed/i.test(
      e.message,
    )
  );
}

/** `reachable` is true when a probe of the API address got any answer at all. */
export function networkMessage(apiUrl: string, reachable: boolean): string {
  if (reachable) {
    return (
      `The browser blocked the request to ${apiUrl} (CORS / preflight). ` +
      "The backend is running, but it does not allow this page's origin. Add the origin " +
      "to the backend CORS settings and restart it."
    );
  }
  return (
    `Cannot reach the backend at ${apiUrl}. It is not running or the address is wrong. ` +
    "Start the backend, or set VITE_API_URL to its address."
  );
}

const MIGRATION_MARKERS =
  /does not exist|no such table|no such column|undefinedtable|undefinedcolumn|undefined column/i;

/** An actionable suffix for statuses whose cause the person can fix, else null. */
export function hintFor(status: number, message: string): string | null {
  if (status >= 500 && MIGRATION_MARKERS.test(message)) {
    return (
      "The database schema is out of date (missing migration). " +
      "Run `alembic upgrade head` in the backend folder and restart it."
    );
  }
  if (status === 401) {
    return (
      "Authentication failed: the credentials or token were not accepted. " +
      "Check the saved token and its permissions."
    );
  }
  if (status === 403) {
    return (
      "Access denied: the credentials or token do not have permission for this action. " +
      "Check the saved token and its scopes."
    );
  }
  if (status === 502 || status === 503 || status === 504) {
    return "The backend or a service in front of it is unavailable. Check that it is running.";
  }
  return null;
}

export function describeFailure(status: number, message: string): string {
  const safe = redactSecrets(message);
  const hint = hintFor(status, safe);
  if (hint) return `${safe} ${hint}`.trim();
  if (status >= 500 && /^Request failed/.test(safe)) {
    return `${safe}. The backend hit an internal error; check its log.`;
  }
  return safe;
}

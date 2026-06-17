### Title
Static AES-256-GCM Key Derived from Empty-String Fallback Allows Decryption of Pyth Pro API Key Cookies — (`File: apps/developer-hub/src/cookies/initialAccessTokenCookie.ts`)

### Summary

The Pyth developer-hub encrypts Pyth Pro API keys (access tokens) into short-lived HTTP cookies using AES-256-GCM. The encryption key is derived **once at module load time** via `scryptSync` with a **hardcoded static salt** (`"pyth-hub"`) and a password that **silently falls back to an empty string** when `COOKIE_SIGNING_SECRET` is not set. This makes the AES-256-GCM key a fully deterministic, publicly computable constant in any deployment where the environment variable is absent, allowing any attacker who can observe the cookie value to decrypt it and recover the user's Pyth Pro API key.

---

### Finding Description

In `apps/developer-hub/src/cookies/initialAccessTokenCookie.ts`, the encryption key is derived at module scope:

```typescript
const cookieSigningSecret = process.env.COOKIE_SIGNING_SECRET ?? "";
const key = scryptSync(cookieSigningSecret, "pyth-hub", 32);
``` [1](#0-0) 

Two compounding flaws exist:

1. **Empty-string fallback**: If `COOKIE_SIGNING_SECRET` is not set in the deployment environment, `cookieSigningSecret` is `""`. The resulting key is `scryptSync("", "pyth-hub", 32)` — a value any attacker can compute locally from the open-source code.

2. **Static, hardcoded salt**: The `scryptSync` salt is the literal string `"pyth-hub"`. A proper salt must be random and unique per derivation to prevent precomputation. A fixed salt means the key is entirely determined by the password alone, and if the password is empty, the key is a known constant.

This key is then reused for **every** `encrypt()` call across all users and all requests:

```typescript
function encrypt(value: string): string {
  const iv = randomBytes(16);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  ...
}
``` [2](#0-1) 

The encrypted cookie stores the user's `initialAccessToken` (a Pyth Pro API key), set via the `/api/playground/deeplink` POST endpoint and read back by the `PlaygroundPage` server component: [3](#0-2) [4](#0-3) 

The cookie is named `developer-playground-initial-access-token` and is marked `httpOnly`, `secure`, `sameSite: lax`, with a 60-second `maxAge`. [5](#0-4) 

---

### Impact Explanation

When `COOKIE_SIGNING_SECRET` is not configured (empty string default), the AES-256-GCM key is `scryptSync("", "pyth-hub", 32)` — a constant any attacker can derive from the public source code. Any party who obtains the ciphertext of the cookie (e.g., via server-side log exposure, proxy logs, or a separate path-traversal/SSRF vulnerability on the Next.js server) can:

1. Compute the known key offline.
2. Decrypt the cookie to recover the user's Pyth Pro API key (`initialAccessToken`).
3. Use the stolen API key to impersonate the user against Pyth Pro WebSocket endpoints, consuming their quota or accessing their subscribed price feeds.

Additionally, an attacker with the known key can **forge** valid encrypted cookies, injecting an arbitrary `initialAccessToken` into a victim's playground session if they can influence the cookie jar (e.g., via a subdomain cookie injection or CSRF against the deeplink endpoint).

---

### Likelihood Explanation

- The `?? ""` fallback is a silent no-op when the environment variable is absent; there is no startup assertion or warning that would alert operators to a misconfigured deployment.
- The source code is public, so the fallback key `scryptSync("", "pyth-hub", 32)` is trivially computable by any attacker.
- Server-side log leakage of cookie values is a common secondary vulnerability in Next.js deployments (e.g., middleware logging, error reporting tools like Sentry capturing request headers).
- The static salt `"pyth-hub"` means even a correctly configured deployment with a weak or reused `COOKIE_SIGNING_SECRET` is vulnerable to precomputation attacks.

---

### Recommendation

**Short term:**
- Add a startup assertion that rejects an empty `COOKIE_SIGNING_SECRET`:
  ```typescript
  if (!cookieSigningSecret) throw new Error("COOKIE_SIGNING_SECRET must be set");
  ```
- Replace the hardcoded salt `"pyth-hub"` with a randomly generated, per-derivation salt stored alongside the ciphertext (analogous to how the IV is already prepended to the ciphertext).

**Long term:**
- Consider using a proper AEAD envelope: derive a fresh key per encryption using a random salt, and store `[salt | iv | authTag | ciphertext]` in the cookie.
- Audit all other uses of `process.env.*` with `?? ""` fallbacks in security-sensitive contexts across the developer-hub.
- Add integration tests that assert encryption fails loudly when the secret is absent.

---

### Proof of Concept

```typescript
import { scryptSync, createDecipheriv } from "node:crypto";

// Step 1: Derive the known key (empty string fallback + static salt)
const key = scryptSync("", "pyth-hub", 32);

// Step 2: Obtain the cookie value (base64) from logs/proxy/etc.
const cookieB64 = "<observed developer-playground-initial-access-token cookie value>";
const data = Buffer.from(cookieB64, "base64");

// Step 3: Parse the cookie format: [iv(16) | authTag(16) | ciphertext]
const iv = data.subarray(0, 16);
const authTag = data.subarray(16, 32);
const encrypted = data.subarray(32);

// Step 4: Decrypt
const decipher = createDecipheriv("aes-256-gcm", key, iv);
decipher.setAuthTag(authTag);
const plaintext = decipher.update(encrypted) + decipher.final("utf8");

// plaintext contains: {"initialAccessToken":"<PYTH_PRO_API_KEY>"}
console.log(plaintext);
``` [6](#0-5)

### Citations

**File:** apps/developer-hub/src/cookies/initialAccessTokenCookie.ts (L13-22)
```typescript
const cookieSigningSecret = process.env.COOKIE_SIGNING_SECRET ?? "";

const INITIAL_ACCESS_TOKEN_COOKIE_NAME =
  "developer-playground-initial-access-token" as const;

const initialAccessTokenSchema = z.object({
  initialAccessToken: z.string().optional().nullable(),
});

const key = scryptSync(cookieSigningSecret, "pyth-hub", 32);
```

**File:** apps/developer-hub/src/cookies/initialAccessTokenCookie.ts (L24-33)
```typescript
function encrypt(value: string): string {
  const iv = randomBytes(16);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  const encrypted = Buffer.concat([
    cipher.update(value, "utf8"),
    cipher.final(),
  ]);
  const authTag = cipher.getAuthTag();
  return Buffer.concat([iv, authTag, encrypted]).toString("base64");
}
```

**File:** apps/developer-hub/src/cookies/initialAccessTokenCookie.ts (L35-47)
```typescript
function decrypt(value: string): string | null {
  try {
    const data = Buffer.from(value, "base64");
    const iv = data.subarray(0, 16);
    const authTag = data.subarray(16, 32);
    const encrypted = data.subarray(32);
    const decipher = createDecipheriv("aes-256-gcm", key, iv);
    decipher.setAuthTag(authTag);
    return decipher.update(encrypted) + decipher.final("utf8");
  } catch {
    return null;
  }
}
```

**File:** apps/developer-hub/src/cookies/initialAccessTokenCookie.ts (L69-81)
```typescript
      const c = await cookies();
      c.set(
        INITIAL_ACCESS_TOKEN_COOKIE_NAME,
        encrypt(JSON.stringify(parsed.data)),
        {
          httpOnly: true,
          maxAge: 60, // 60 seconds
          path: "/playground",
          sameSite: "lax",
          secure: true,
        },
      );
    }
```

**File:** apps/developer-hub/src/app/api/playground/deeplink/route.ts (L1-8)
```typescript
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { Cookies } from "../../../../cookies/initialAccessTokenCookie";

export async function POST(request: NextRequest) {
  await Cookies.setInitialAccessToken(request);
  return NextResponse.redirect(new URL("/playground", request.url), { status: 303 });
}
```

**File:** apps/developer-hub/src/components/Pages/PlaygroundPage/index.tsx (L5-13)
```typescript
export async function PlaygroundPage() {
  /** server data */
  const tokenData = await Cookies.getInitialAccessToken();

  return (
    <PlaygroundProvider
      initialConfig={{
        accessToken: tokenData?.initialAccessToken ?? "",
      }}
```

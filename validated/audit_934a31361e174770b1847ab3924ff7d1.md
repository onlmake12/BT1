### Title
IP Spoofing Bypasses Rate Limiter, Enabling Unlimited Depletion of Shared Pyth Pro Demo Token - (File: `apps/developer-hub/src/app/api/playground/stream/route.ts`)

### Summary
The `/api/playground/stream` endpoint protects the shared server-side `PYTH_PRO_DEMO_TOKEN` with a per-IP rate limiter. However, the IP extraction function blindly trusts the attacker-controlled `X-Forwarded-For` header, taking its first value as the client identity. An unprivileged attacker can rotate this header across requests to bypass the rate limit entirely, opening unlimited upstream WebSocket connections to `wss://pyth-lazer.dourolabs.app/v1/stream` and exhausting the shared demo token's quota.

### Finding Description

The `getClientIp` function in the stream route reads the client IP from the `x-forwarded-for` header and takes the first comma-separated value: [1](#0-0) 

This value is then used as the rate-limit key: [2](#0-1) 

The in-memory rate limiter stores per-key timestamps in a plain `Map`: [3](#0-2) 

Because `X-Forwarded-For` is a client-supplied header, an attacker can set it to an arbitrary value on every request (e.g., `X-Forwarded-For: 1.1.1.1`, then `X-Forwarded-For: 2.2.2.2`, etc.). Each unique value is treated as a new client identity, so the 5-requests-per-minute window is never triggered for any single "identity."

Even on platforms like Vercel that append the real IP, they do not strip existing `X-Forwarded-For` values. The code takes `split(",")[0]`, which is the attacker-controlled leftmost entry, not the platform-appended real IP.

The rate limit is only applied when `usesDemoToken` is true (no `accessToken` provided): [4](#0-3) 

When the rate limit is bypassed, each accepted request opens a new WebSocket connection to the upstream Pyth Pro endpoint using the server's `PYTH_PRO_DEMO_TOKEN`: [5](#0-4) 

The demo token is configured server-side: [6](#0-5) 

Each stream is held open for up to `PLAYGROUND_MAX_STREAM_DURATION_MS` (default 60 seconds): [7](#0-6) 

A secondary amplification exists: `priceFeedIds` in `StreamRequestSchema` has only a `.min(1)` constraint and no `.max()`, so each request can subscribe to an arbitrarily large number of feeds, increasing upstream load per connection: [8](#0-7) 

The in-memory store also grows unboundedly with unique spoofed IPs, as cleanup only runs every 5 minutes: [9](#0-8) 

### Impact Explanation
An attacker can exhaust the shared `PYTH_PRO_DEMO_TOKEN`'s upstream quota on the Pyth Pro (Lazer) WebSocket service, making the playground demo unavailable to all legitimate users. Simultaneously, the unbounded growth of the `rateLimitStore` Map with unique spoofed IPs can cause memory pressure on the Next.js server process, degrading or crashing the developer hub.

### Likelihood Explanation
The attack requires only HTTP requests with a rotating `X-Forwarded-For` header — no authentication, no special tooling, and no privileged access. The endpoint is publicly reachable. The bypass is trivially scriptable.

### Recommendation
1. **Do not trust client-supplied forwarding headers for rate limiting.** Use the platform's verified real IP (e.g., `request.ip` on Vercel, or the rightmost non-private IP in a validated `X-Forwarded-For` chain set by a trusted proxy).
2. **Add an upper bound on `priceFeedIds`** in `StreamRequestSchema` (e.g., `.max(50)`).
3. **Replace the in-memory rate limiter** with a distributed store (Redis/Upstash) as the code's own comment already recommends, to prevent Map growth and to enforce limits across serverless instances.

### Proof of Concept
```bash
# Bypass rate limit by rotating the X-Forwarded-For header on each request
for i in $(seq 1 100); do
  curl -s -X POST https://<developer-hub>/api/playground/stream \
    -H "Content-Type: application/json" \
    -H "X-Forwarded-For: 10.0.0.$i" \
    -d '{
      "priceFeedIds": [1,2,3,4,5,6,7,8,9,10],
      "properties": ["price"],
      "formats": ["solana"],
      "channel": "real_time",
      "deliveryFormat": "json"
    }' &
done
# Each request passes the rate limit check with a unique "IP",
# opens a 60-second upstream WebSocket using PYTH_PRO_DEMO_TOKEN,
# and depletes the demo token's upstream quota.
```

### Citations

**File:** apps/developer-hub/src/app/api/playground/stream/route.ts (L28-30)
```typescript
  priceFeedIds: z
    .array(z.number())
    .min(1, "At least one price feed ID required"),
```

**File:** apps/developer-hub/src/app/api/playground/stream/route.ts (L50-60)
```typescript
function getClientIp(request: NextRequest): string {
  const forwardedFor = request.headers.get("x-forwarded-for");
  if (forwardedFor) {
    return forwardedFor.split(",")[0]?.trim() ?? "unknown";
  }
  const realIp = request.headers.get("x-real-ip");
  if (realIp) {
    return realIp;
  }
  return "unknown";
}
```

**File:** apps/developer-hub/src/app/api/playground/stream/route.ts (L105-116)
```typescript
  // Determine which token to use
  const demoToken = PYTH_PRO_DEMO_TOKEN;
  const usesDemoToken = !config.accessToken;

  if (usesDemoToken && !demoToken) {
    return new Response(
      JSON.stringify({ error: "Demo token not configured on server" }),
      { status: 500, headers: { "Content-Type": "application/json" } },
    );
  }

  const accessToken = usesDemoToken ? demoToken : config.accessToken;
```

**File:** apps/developer-hub/src/app/api/playground/stream/route.ts (L119-143)
```typescript
  if (usesDemoToken) {
    const clientIp = getClientIp(request);
    const rateLimitResult = checkRateLimit(clientIp, {
      windowMs: PLAYGROUND_RATE_LIMIT_WINDOW_MS,
      maxRequests: PLAYGROUND_RATE_LIMIT_MAX_REQUESTS,
    });

    if (!rateLimitResult.allowed) {
      const retryAfterSeconds = Math.ceil(rateLimitResult.resetIn / 1000);
      return new Response(
        JSON.stringify({
          error: "Rate limit exceeded",
          message: `Too many requests. Try again in ${String(retryAfterSeconds)} seconds.`,
          resetIn: rateLimitResult.resetIn,
        }),
        {
          status: 429,
          headers: {
            "Content-Type": "application/json",
            "Retry-After": String(retryAfterSeconds),
          },
        },
      );
    }
  }
```

**File:** apps/developer-hub/src/app/api/playground/stream/route.ts (L191-197)
```typescript
        const wsUrl = PYTH_PRO_WS_ENDPOINT;
        const wsOptions = {
          headers: {
            Authorization: `Bearer ${accessToken ?? ""}`,
          },
        };
        websocket = new WebSocket(wsUrl, wsOptions);
```

**File:** apps/developer-hub/src/lib/rate-limiter.ts (L27-27)
```typescript
const rateLimitStore = new Map<string, RateLimitEntry>();
```

**File:** apps/developer-hub/src/lib/rate-limiter.ts (L30-49)
```typescript
const CLEANUP_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes

let cleanupInterval: ReturnType<typeof setInterval> | undefined;

function startCleanup(windowMs: number) {
  if (cleanupInterval) return;

  cleanupInterval = setInterval(() => {
    const now = Date.now();
    for (const [key, entry] of rateLimitStore.entries()) {
      // Remove timestamps older than the window
      entry.timestamps = entry.timestamps.filter(
        (timestamp) => now - timestamp < windowMs,
      );
      // Remove entry if no timestamps remain
      if (entry.timestamps.length === 0) {
        rateLimitStore.delete(key);
      }
    }
  }, CLEANUP_INTERVAL_MS);
```

**File:** apps/developer-hub/src/config/pyth-pro.ts (L17-17)
```typescript
export const PYTH_PRO_DEMO_TOKEN = process.env.PYTH_PRO_DEMO_TOKEN;
```

**File:** apps/developer-hub/src/config/pyth-pro.ts (L20-22)
```typescript
export const PLAYGROUND_MAX_STREAM_DURATION_MS = Number.parseInt(
  getEnvOrDefault("PLAYGROUND_MAX_STREAM_DURATION_MS", "60000"),
  10,
```

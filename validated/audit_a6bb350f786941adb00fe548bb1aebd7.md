Audit Report

## Title
Self-Referential Hole Punching Messages Allow Resource Exhaustion via Unbounded Background Task Spawning — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

## Summary

An attacker connected to a victim CKB node can send a `ConnectionRequest` with `from = victim_peer_id` and `to = victim_peer_id`. Because no guard rejects self-referential messages, `respond_delivered` inserts attacker-controlled addresses into `pending_delivered` under the victim's own peer ID. A subsequent stream of `ConnectionSync(from=victim_id, to=victim_id, route=[])` messages (rate-limited to 1/second) each cause the victim to spawn up to 24 background tasks calling `try_nat_traversal`, each retrying TCP connections for 30 seconds. After 30 seconds, up to 720 concurrent background tasks are active, exhausting file descriptors and thread pool capacity, which can crash the node.

## Finding Description

**Step 1 — Poison `pending_delivered`**

In `ConnectionRequestProcess::execute()`, the only guards before calling `respond_delivered` are:
- `content.route.contains(self_peer_id)` — passes with an empty route
- `forward_rate_limiter.check_key(&(content.from, content.to, item_id))` — allows 1/second for the key `(victim_id, victim_id, item_id)`
- `self_peer_id == &content.to` — is `true` when `to = victim_id`

There is no check that `content.from != self_peer_id`. When the attacker sends `from = victim_id, to = victim_id`, the condition at line 145 is satisfied and `respond_delivered(from_peer_id = victim_id, listen_addrs = [attacker_ip:port])` is called.

Inside `respond_delivered`, after a 2-minute cooldown check (lines 161–167) and TCP/IPv4/IPv6 address filtering (lines 196–215), the function sends a `ConnectionRequestDelivered` back to the attacker's session (lines 226–232), then unconditionally inserts:

```rust
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
```

at lines 234–237. This stores `victim_id → [attacker_ip:port, ...]` in `pending_delivered`.

**Step 2 — Trigger `try_nat_traversal` against attacker addresses**

In `ConnectionSyncProcess::execute()`, when `route` is empty and `self_peer_id == content.to` (both true when `from = to = victim_id`), the code at lines 111–115 does:

```rust
let listens_info = self.protocol.pending_delivered
    .get(&content.from)
    .map(|info| info.0.clone());
```

With `content.from = victim_id`, this retrieves the attacker-controlled addresses. The entry is **not removed** — `get()` is used, not `remove()`. Each address is passed to `try_nat_traversal` in a spawned background task (lines 119–162). The spawn only requires `listen_addresses.first()` to be `Some`, which is true for any listening node.

**`try_nat_traversal` behavior** (`component/mod.rs` lines 49–115): Each task retries TCP connections to the target address for up to 30 seconds with ~200ms intervals (~150 attempts per address).

**Rate limiting does not prevent the attack:**

The `forward_rate_limiter` is keyed by `(from, to, item_id)` at 1/second. With `from = to = victim_id`, the attacker can send 1 `ConnectionSync` per second. Each spawns up to `ADDRS_COUNT_LIMIT = 24` background tasks. After 30 seconds: **30 × 24 = 720 concurrent background tasks**, each holding a socket and retrying for 30 seconds.

The `pending_delivered` 2-minute cooldown (lines 161–167 of `connection_request.rs`) only prevents re-poisoning, not re-triggering via `ConnectionSync`. The top-level `rate_limiter` (keyed by `(session_id, item_id)` at 30/second) is not the binding constraint.

## Impact Explanation

The unbounded spawning of background tasks exhausts file descriptors, thread pool capacity, and CPU on the victim node, causing it to crash or become unresponsive. This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

Any peer connected to the victim over the P2P network can execute this attack. No special privileges, keys, or majority hashpower are required. The `from` and `to` fields in `ConnectionRequest` and `ConnectionSync` are unauthenticated byte fields — any peer can set them to any value, including the victim's own `local_peer_id()`. The attack is locally testable and requires only two crafted P2P message types. The 1/second rate limit on `ConnectionSync` does not prevent the attack; it only paces the task accumulation.

## Recommendation

Add an explicit guard in `ConnectionRequestProcess::execute()` rejecting messages where `content.from == self_peer_id` or `content.from == content.to`, placed before the `self_peer_id == &content.to` branch:

```rust
if &content.from == self_peer_id || content.from == content.to {
    return StatusCode::Ignore.with_context("self-referential request rejected");
}
```

Add a symmetric guard in `ConnectionSyncProcess::execute()` rejecting `content.from == self_peer_id` when the node is the `to` target. Additionally, consider using `remove()` instead of `get()` in `ConnectionSyncProcess` so that each `pending_delivered` entry can only trigger one round of NAT traversal tasks.

## Proof of Concept

```rust
// Pseudocode unit test
let victim_peer_id = node.local_peer_id().clone();

// Step 1: Send ConnectionRequest(from=victim, to=victim, listen_addrs=[attacker:1337])
// Precondition: attacker is connected to victim
let req = ConnectionRequest {
    from: victim_peer_id.clone(),
    to: victim_peer_id.clone(),
    listen_addrs: vec!["/ip4/1.2.3.4/tcp/1337".parse().unwrap()],
    route: vec![],  // empty route bypasses route check
    max_hops: 6,
};
attacker_session.send(req);
// Assert: pending_delivered[victim_peer_id] == [/ip4/1.2.3.4/tcp/1337/p2p/<victim>]

// Step 2: Send 30 ConnectionSync(from=victim, to=victim, route=[]) at 1/second
for _ in 0..30 {
    let sync = ConnectionSync {
        from: victim_peer_id.clone(),
        to: victim_peer_id.clone(),
        route: vec![],
    };
    attacker_session.send(sync);
    sleep(Duration::from_secs(1));
}
// Assert: 720 concurrent try_nat_traversal tasks active
// Assert: TCP SYN packets observed toward 1.2.3.4:1337
// Assert: node file descriptor count approaches system limit
```
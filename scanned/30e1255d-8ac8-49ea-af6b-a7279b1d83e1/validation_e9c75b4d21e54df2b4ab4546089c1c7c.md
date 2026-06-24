Audit Report

## Title
Unvalidated Attacker-Controlled `listen_addrs` in Hole-Punching Protocol Enables Node Crash via Async Task Exhaustion — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

An unprivileged peer with a standard P2P connection can cause the victim node to spawn unbounded 30-second async tasks by sending crafted `ConnectionRequest` and `ConnectionSync` messages with arbitrary `listen_addrs`. The `from` field in both messages is never verified against the actual session peer ID, allowing the attacker to rotate synthetic peer IDs to bypass all per-key cooldowns and rate limiters. At the per-session rate limit of 30 messages/second, an attacker can sustain ~720 concurrent `try_nat_traversal` tasks per second (21,600 at steady state), exhausting async task resources and crashing the node.

## Finding Description

**Root cause — no IP filtering and no session-to-message binding:**

In `respond_delivered()` (`connection_request.rs` L196–215), attacker-supplied `listen_addrs` are filtered only by transport type (TCP) and IPv4/IPv6 presence. No private, loopback, or link-local address check exists:

```rust
TransportType::Tcp => {
    if addr.iter().any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_))) {
        Some(addr)
    } else {
        None
    }
}
```

These addresses are then stored verbatim in `pending_delivered`, keyed by `content.from` — a message-level field, never verified against the actual session peer ID (`connection_request.rs` L234–237):

```rust
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
```

`ConnectionSyncProcess` (`connection_sync.rs`) has no `peer` field and performs no session-to-`from` binding check. When `content.to == self_peer_id`, it retrieves the stored addresses by `content.from` and spawns one `try_nat_traversal` task per address (`connection_sync.rs` L111–124):

```rust
let listens_info = self.protocol.pending_delivered.get(&content.from)...
let tasks = listens.into_iter()
    .map(|listen_addr| Box::pin(try_nat_traversal(self.bind_addr, listen_addr)))
    .collect::<Vec<_>>();
```

`try_nat_traversal` (`component/mod.rs` L49–115) runs a retry loop for up to 30 seconds (~150 TCP SYN attempts per address at 200ms intervals) with no IP-range filtering.

**Rate limiter bypass:**

Three guards exist but all are bypassable:

1. **Per-session rate limiter** (`mod.rs` L95–107): 30 req/sec per `(session_id, msg_item_id)`. This is the binding constraint — it allows 30 `ConnectionRequest` + 30 `ConnectionSync` per second from one connection.
2. **`forward_rate_limiter`** (`connection_request.rs` L132–143, `connection_sync.rs` L85–96): 1/sec per `(from, to, msg_item_id)`. Fully bypassed by rotating `from` peer IDs.
3. **`HOLE_PUNCHING_INTERVAL`** (`connection_request.rs` L161–167): 2-minute cooldown per `from_peer_id`. Fully bypassed by rotating `from` peer IDs.

**Exploit arithmetic:**

- 30 `ConnectionRequest`/sec × 24 addresses each → 30 new `pending_delivered` entries/sec
- 30 `ConnectionSync`/sec → 30 × 24 = **720 `try_nat_traversal` tasks spawned/sec**
- Each task lives 30 seconds → **21,600 concurrent tasks at steady state** from a single attacker connection

## Impact Explanation

This is a **High** severity vulnerability matching: *"Vulnerabilities which could easily crash a CKB node"* (10001–15000 points).

Spawning 21,600 concurrent 30-second async tasks from a single P2P connection exhausts the Tokio runtime's task capacity and system socket/file-descriptor limits, causing the node process to crash or become unresponsive. Multiple attacker connections multiply the effect linearly. The victim node is taken fully offline, disrupting its participation in block propagation and transaction relay.

## Likelihood Explanation

Preconditions are minimal: a standard P2P connection to the victim (no special privileges) and knowledge of the victim's peer ID (publicly discoverable via DHT/peer exchange). The two-message sequence is trivial to craft. The attacker needs only to generate fresh random `PeerId` bytes for each `from` field — syntactic validity is the only requirement (`PeerId::from_bytes` at `connection_request.rs` L36–38). The attack is repeatable, automatable, and effective from a single connection.

## Recommendation

1. **Reject private/loopback/link-local addresses** in `respond_delivered()` before inserting into `pending_delivered`. Add a filter on `Protocol::Ip4(addr)` checking `addr.is_private() || addr.is_loopback() || addr.is_link_local()` and equivalent IPv6 checks.
2. **Bind `pending_delivered` to the actual session peer ID**, not `content.from`. Resolve the session peer ID from `self.peer` (already available in `ConnectionRequestProcess`) and use it as the map key.
3. **Verify the `ConnectionSync` sender** by passing the session peer ID into `ConnectionSyncProcess` and asserting it matches the `content.from` lookup key.
4. **Cap concurrent `try_nat_traversal` tasks** with a semaphore or bounded task pool to limit blast radius even if other guards are bypassed.

## Proof of Concept

```
1. Attacker establishes a standard P2P connection to victim.

2. For i in 1..30 (per second, within rate limit):
   Send ConnectionRequest {
     from: PeerId::random(),   // fresh ID each iteration
     to:   <victim_peer_id>,
     max_hops: 6,
     route: [],
     listen_addrs: [
       /ip4/192.168.1.1/tcp/8114,   // internal RPC
       /ip4/10.0.0.1/tcp/22,        // internal SSH
       /ip4/169.254.169.254/tcp/80, // cloud metadata
       ... (24 addresses total)
     ]
   }
   // victim stores 24 addresses in pending_delivered[PeerId::random()]

3. For i in 1..30 (per second):
   Send ConnectionSync {
     from: <same PeerId used in step 2, iteration i>,
     to:   <victim_peer_id>,
     route: []
   }
   // victim spawns 24 try_nat_traversal tasks × 30 = 720 tasks/sec

4. After ~30 seconds: ~21,600 concurrent tasks running on victim's Tokio runtime.
   Node becomes unresponsive / OOMs / crashes.

Verification: monitor victim's process memory and open socket count;
both grow linearly until crash. A unit test can mock the rate limiter
and assert task count exceeds a threshold after N message pairs.
```
### Title
Hole-Punching `ConnectionRequest` Amplification via Unique Spoofed (from, to) Pairs — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

An unprivileged remote peer can inject `ConnectionRequest` messages with unique spoofed `(from, to)` PeerId pairs targeting a nonexistent `to` peer. Each message causes every receiving node to `filter_broadcast` to `sqrt(N)` peers. Because the `forward_rate_limiter` is **per-node** (not global), each intermediate node sees the `(from, to)` key for the first time and forwards. The result is an O(N^1.5) message amplification per injected message, not the O(N^3) claimed — but still a concrete, exploitable amplification attack.

---

### Finding Description

**Entry point — attacker's direct connection:**

The `received` handler applies a per-session rate limiter keyed on `(session_id, msg_item_id)` at 30 req/sec: [1](#0-0) 

This allows the attacker to inject 30 unique `ConnectionRequest` messages per second.

**`execute()` — forward path when `to` is not found:**

When `self_peer_id != content.to` and `max_hops > 0`, `forward_message` is called: [2](#0-1) 

**`forward_message()` — the fan-out:**

When `target_sid` is `None` (nonexistent `to` peer), the node broadcasts to `sqrt(N)` peers: [3](#0-2) 

**The `forward_rate_limiter` is per-node, not global:**

The limiter is keyed on `(content.from, content.to, self.msg_item_id)` and allows 1 req/sec: [4](#0-3) 

Each `HolePunching` instance owns its own `forward_rate_limiter`. When Node B receives a forwarded message with key `(X, Y, type)`, it checks **its own** rate limiter — which has never seen `(X, Y, type)` before — and passes. The rate limiter only prevents a single node from forwarding the same `(from, to)` pair more than once per second; it does not prevent N different nodes from each forwarding it once.

**The route check prevents loops, not fan-out:** [5](#0-4) 

The route only contains the specific chain of forwarders for one copy of the message. At hop k, the route has k entries. A node not in that chain will still forward. Multiple copies of the same message (with different routes) can reach the same node, but the per-node `forward_rate_limiter` will drop the second copy — meaning each node forwards at most once per second per `(from, to)` pair.

**Corrected amplification bound:**

The per-node rate limiter caps each of the N nodes to forwarding a given `(from, to)` pair at most once per second. Each forwarding sends to `sqrt(N)` peers. Total messages per attacker message = **N × sqrt(N) = O(N^1.5)**, not O(N^3) as claimed. With 30 unique `(from, to)` pairs per second:

| N nodes | Messages/sec (attacker injects 30/sec) |
|---------|----------------------------------------|
| 1,000   | 30 × 1,000 × 31 ≈ **930,000**         |
| 10,000  | 30 × 10,000 × 100 = **30,000,000**    |

This is a **~31,000× to ~1,000,000× amplification factor** from a single attacker connection.

---

### Impact Explanation

Every node in the network receives and re-broadcasts each attacker-injected message. At N=10,000 nodes, 30 attacker messages/sec generate 30 million protocol messages/sec network-wide, saturating P2P bandwidth and starving legitimate traffic. The `HolePunching` protocol handler is single-threaded per node (async but sequential per session), so message queue saturation also stalls other protocol processing.

---

### Likelihood Explanation

- Requires only one direct P2P connection to any CKB node — no authentication, no stake, no PoW.
- The spoofed `from`/`to` PeerIds need only be syntactically valid bytes; they need not correspond to real peers.
- The `listen_addrs` field must be non-empty and contain a valid TCP multiaddr — trivially satisfied.
- The attack is repeatable indefinitely; the attacker is never banned (no `should_ban()` path is triggered for `TooManyRequests` or `ReachedMaxHops`). [6](#0-5) 

---

### Recommendation

1. **Global message deduplication:** Maintain a shared, bounded LRU cache of recently-seen `(from, to, hash(message))` tuples across all nodes' forwarding decisions, or use a network-wide nonce/message-ID field that intermediate nodes cache and deduplicate on.
2. **Bound the `forward_rate_limiter` globally:** Move the `forward_rate_limiter` to a shared `Arc<Mutex<...>>` so that all sessions on a node share a single budget per `(from, to)` pair — this already exists per-node but needs to also account for messages arriving from different peers.
3. **Reduce `MAX_HOPS` or the broadcast fan-out:** The `sqrt(N)` fan-out combined with `MAX_HOPS=6` is the root cause of the amplification. Capping the fan-out to a small constant (e.g., 3–5 peers) regardless of N would bound the amplification.
4. **Ban on repeated nonexistent-target forwarding:** Track per-session counts of forwarded messages where `target_sid` was `None`; ban sessions that exceed a threshold.

---

### Proof of Concept

```
1. Attacker establishes one TCP connection to any CKB mainnet node (Node A).
2. In a loop at 30 msg/sec, attacker sends:
     ConnectionRequest {
       from: random_peer_id(),   // unique each iteration
       to:   random_peer_id(),   // unique each iteration, not in any registry
       max_hops: 6,
       route: [],
       listen_addrs: ["/ip4/1.2.3.4/tcp/8115/p2p/<from_peer_id>"]
     }
3. Node A: rate_limiter[(session_A, ConnectionRequest_type)] passes (≤30/sec).
4. Node A: forward_rate_limiter[(from_i, to_i, type)] passes (first time for this unique pair).
5. Node A: target_sid = None → filter_broadcast to sqrt(N) peers.
6. Each of those sqrt(N) peers: forward_rate_limiter[(from_i, to_i, type)] passes (first time on that node).
7. Each broadcasts to sqrt(N) more peers, and so on up to MAX_HOPS=6 or until all N nodes have forwarded.
8. Instrument: assert total_messages_sent_network_wide <= O(1). Actual: O(N^1.5) per attacker message.
9. At N=10,000: 30 injected msg/sec → ~30,000,000 forwarded msg/sec observed across all nodes.
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L145-166)
```rust
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, session_id, ban_time, status
            );
            self.network_state.ban_session(
                &context.control().clone().into(),
                session_id,
                ban_time,
                status.to_string(),
            );
        } else if status.should_warn() {
            warn!(
                "process {} from {}; result is {}",
                item_name, session_id, status
            );
        } else if !status.is_ok() {
            debug!(
                "process {} from {}; result is {}",
                item_name, session_id, status
            );
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L127-130)
```rust
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-152)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
            StatusCode::ReachedMaxHops.into()
        } else {
            self.forward_message(self_peer_id, &content.to).await
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L273-305)
```rust
            None => {
                debug!(
                    "target peer {} is not found, broadcast the request to more peers",
                    to_peer_id
                );

                // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
                let sid = self.peer;
                let mut total = self
                    .protocol
                    .network_state
                    .with_peer_registry(|p| p.peers().len())
                    .isqrt();
                if let Err(error) = self
                    .p2p_control
                    .filter_broadcast(
                        TargetSession::Filter(Box::new(move |id| {
                            if id == &sid {
                                return false;
                            }
                            total = total.saturating_sub(1);
                            total != 0
                        })),
                        proto_id,
                        new_message,
                    )
                    .await
                {
                    StatusCode::BroadcastError.with_context(error)
                } else {
                    Status::ok()
                }
            }
```

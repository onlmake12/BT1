The code in question is directly readable. Let me analyze the exact logic at lines 279–305.

The off-by-one is real and present in the code. Let me trace it precisely.

---

### Title
Gossip Broadcast Off-by-One in `forward_message` Sends to Zero Peers When `isqrt(total) == 1` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary

`ConnectionRequestProcess::forward_message` computes `total = peers().len().isqrt()` as the gossip fan-out count, then uses a `filter_broadcast` closure that **decrements before checking**. When `total` starts at 1 (i.e., 1–3 connected peers), the first eligible candidate immediately drives `total` to 0 and returns `false`, so zero peers receive the forwarded `ConnectionRequest`. The function still returns `Status::ok()`, making the failure silent.

### Finding Description

The faulty closure is at lines 289–295:

```rust
TargetSession::Filter(Box::new(move |id| {
    if id == &sid {
        return false;
    }
    total = total.saturating_sub(1);   // decrement first
    total != 0                          // then check — already 0 when total started at 1
})),
``` [1](#0-0) 

Execution trace for `peers().len()` ∈ {1, 2, 3} → `isqrt()` = 1:

| Iteration | `id == sid`? | `total` before | `total` after `saturating_sub(1)` | Return |
|-----------|-------------|----------------|-----------------------------------|--------|
| 1st eligible peer | no | 1 | 0 | `false` (excluded) |

Result: **0 peers** receive the message. The intended result is 1.

For `peers().len()` ∈ {4..8} → `isqrt()` = 2, only 1 peer receives instead of 2. The off-by-one is universal; it always delivers to `isqrt(n) - 1` peers.

The identical pattern also appears in the `notify` path (self-initiated hole-punching): [2](#0-1) 

### Impact Explanation

Any unprivileged remote peer can send a well-formed `ConnectionRequest` with an unknown `to` peer ID to a node that has 1–3 total connections. The receiving node enters the gossip broadcast branch, computes `total = 1`, and the closure immediately returns `false` for the first candidate, forwarding to nobody. The function returns `Status::ok()` — no error, no log at warn/error level — so the hole-punching attempt silently dies. Nodes with few connections (common during startup or in sparse network regions) cannot relay hole-punching requests at all. [3](#0-2) 

### Likelihood Explanation

The precondition (`peers().len()` ≤ 3) is realistic: any node during bootstrap, or a node in a sparse region, will have 1–3 peers. No special privilege is required — any connected peer can send a `ConnectionRequest`. The `HolePunching` protocol is enabled in production and the message is accepted after only rate-limit and TTL checks. [4](#0-3) 

### Recommendation

Decrement **after** the guard check, not before:

```rust
// Fix: check first, then consume the budget
TargetSession::Filter(Box::new(move |id| {
    if id == &sid || total == 0 {
        return false;
    }
    total -= 1;
    true
}))
```

Apply the same fix to the identical closure in `notify` at `mod.rs` lines 227–230.

### Proof of Concept

1. Start node A with exactly 1 peer (node B) connected.
2. From node B, send a `ConnectionRequest` where `to` is an unknown peer ID and `max_hops > 0`.
3. Node A: `peers().len() = 1`, `isqrt(1) = 1`, `total = 1`.
4. `filter_broadcast` closure fires for the one candidate peer (B is excluded by `id == &sid`; if there were a third peer C, it would be the candidate): `total = 0`, returns `false`.
5. Assert: no peer receives the forwarded `ConnectionRequest` — confirmed by the closure logic above.
6. Expected: at least 1 peer should receive it.

The same assertion fails for 2 or 3 total peers. The `Status::ok()` return means the caller observes no error. [5](#0-4)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L279-305)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L109-120)
```rust
        let status = match msg {
            packed::HolePunchingMessageUnionReader::ConnectionRequest(reader) => {
                component::ConnectionRequestProcess::new(
                    reader,
                    self,
                    context.session.id,
                    context.control(),
                    msg.item_id(),
                )
                .execute()
                .await
            }
```

**File:** network/src/protocols/hole_punching/mod.rs (L224-234)
```rust
                    let mut total = status.total.isqrt();
                    let _ignore = context
                        .filter_broadcast(
                            TargetSession::Filter(Box::new(move |_| {
                                total = total.saturating_sub(1);
                                total != 0
                            })),
                            proto_id,
                            conn_req.as_bytes(),
                        )
                        .await;
```

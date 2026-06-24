The code confirms all factual claims in the report. Let me verify one more detail about whether the light client protocol has any other guards.

Audit Report

## Title
Missing Per-Peer Rate Limiting on Light Client Protocol Messages Enables Server Resource Exhaustion — (`util/light-client-protocol-server/src/lib.rs`)

## Summary
`LightClientProtocol` contains no rate limiter of any kind. Every `GetLastState` message from any peer unconditionally triggers a snapshot acquisition, two RocksDB point reads, an MMR object construction, and an O(log N) MMR root computation. Other production protocols (`Relayer`, `HolePunching`) both carry a `governor`-based `RateLimiter<(PeerIndex, u32)>` enforcing a 30 req/sec per-peer-per-message-type cap before dispatching. The light client protocol has no equivalent guard, allowing a single unprivileged peer to drive unbounded CPU and DB I/O consumption.

## Finding Description
`LightClientProtocol` is defined with a single field and no rate limiter: [1](#0-0) 

A grep for `rate_limit`, `governor`, `quota`, or `throttle` across the entire `util/light-client-protocol-server/` tree returns zero matches, confirming no guard exists anywhere in the crate.

`received` parses the message and immediately calls `try_process`: [2](#0-1) 

`try_process` dispatches directly to handlers with no rate check: [3](#0-2) 

`GetLastStateProcess::execute()` unconditionally calls `get_verifiable_tip_header()` on every invocation: [4](#0-3) 

`get_verifiable_tip_header()` performs: snapshot acquisition → `tip_hash()` DB read → `get_block()` DB read → `chain_root_mmr(tip_number - 1)` construction → `mmr.get_root()` (O(log N) DB reads): [5](#0-4) 

By contrast, `Relayer::try_process` checks a `governor` rate limiter keyed by `(PeerIndex, message_item_id)` at 30 req/sec before any handler is invoked: [6](#0-5) 

`HolePunching::received` does the same before dispatching: [7](#0-6) 

## Impact Explanation
A single unprivileged peer can send `GetLastState` at wire speed. Each message forces the server to acquire a shared snapshot, perform two RocksDB point reads, construct an MMR over the full chain height, and compute the MMR root (O(log N) additional DB reads). There is no counter, token bucket, or cooldown to bound this per peer. The result is unbounded CPU and DB I/O consumption attributable to one peer, degrading or blocking service for all other peers and the node's own chain-processing tasks. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points), and potentially **High — Vulnerabilities which could easily crash a CKB node** if resource exhaustion is sufficient.

## Likelihood Explanation
The attack requires only a valid P2P connection and the ability to send well-formed `GetLastState` messages in a loop — no PoW, no keys, no special privileges. The `GetLastState` message body is minimal (a single boolean `subscribe` field), so bandwidth cost to the attacker is negligible. The path is directly reachable from any peer on the public network with the light client protocol enabled.

## Recommendation
Add a `governor::RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`, initialize it in `LightClientProtocol::new` with the same 30 req/sec quota used by `Relayer` and `HolePunching`, and check it at the top of `try_process` before dispatching to any handler — mirroring the pattern already established in `sync/src/relayer/mod.rs` lines 116–123 and `network/src/protocols/hole_punching/mod.rs` lines 95–107.

## Proof of Concept
1. Connect a peer to a CKB node with the light client protocol enabled.
2. In a tight loop, send `LightClientMessage { GetLastState { subscribe: false } }` at maximum network speed.
3. Monitor server-side RocksDB read IOPS and CPU usage; both will scale linearly with message rate from that single peer with no upper bound enforced by the server.
4. Confirm that resource consumption is not bounded by any per-peer limit — as proven by the absence of any rate limiter in the entire `util/light-client-protocol-server/` crate.

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-29)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}
```

**File:** util/light-client-protocol-server/src/lib.rs (L79-81)
```rust
        let item_name = msg.item_name();
        let status = self.try_process(&nc, peer, msg).await;
        if let Some(ban_time) = status.should_ban() {
```

**File:** util/light-client-protocol-server/src/lib.rs (L96-125)
```rust
    async fn try_process(
        &mut self,
        nc: &Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        message: packed::LightClientMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::LightClientMessageUnionReader::GetLastState(reader) => {
                components::GetLastStateProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetBlocksProof(reader) => {
                components::GetBlocksProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetTransactionsProof(reader) => {
                components::GetTransactionsProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            _ => StatusCode::UnexpectedProtocolMessage.into(),
        }
    }
```

**File:** util/light-client-protocol-server/src/lib.rs (L127-145)
```rust
    pub(crate) fn get_verifiable_tip_header(&self) -> Result<packed::VerifiableHeader, String> {
        let snapshot = self.shared.snapshot();

        let tip_hash = snapshot.tip_hash();
        let tip_block = snapshot
            .get_block(&tip_hash)
            .expect("checked: tip block should be existed");
        let parent_chain_root = if tip_block.is_genesis() {
            Default::default()
        } else {
            let mmr = snapshot.chain_root_mmr(tip_block.number() - 1);
            match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return Err(errmsg);
                }
            }
        };
```

**File:** util/light-client-protocol-server/src/components/get_last_state.rs (L40-45)
```rust
        let tip_header = match self.protocol.get_verifiable_tip_header() {
            Ok(tip_state) => tip_state,
            Err(errmsg) => {
                return StatusCode::InternalError.with_context(errmsg);
            }
        };
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

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

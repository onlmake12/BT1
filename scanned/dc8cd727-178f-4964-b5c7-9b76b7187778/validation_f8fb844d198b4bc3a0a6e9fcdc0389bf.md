All code references have been verified against the actual codebase. The claim is accurate on every point.

Audit Report

## Title
Missing Per-Peer Rate Limit on `LightClientProtocol` Enables Unbounded MMR Database Reads — (`util/light-client-protocol-server/src/lib.rs`, `util/light-client-protocol-server/src/components/get_last_state.rs`)

## Summary
`LightClientProtocol` contains no rate-limiting mechanism of any kind. Every `GetLastState` message from any peer unconditionally invokes `get_verifiable_tip_header()`, which performs a RocksDB snapshot acquisition, a block-body read, and an O(log N) MMR root read from `COLUMN_CHAIN_ROOT_MMR`. The `Relayer` and `HolePunching` protocols both carry a `governor`-based 30 req/s per-peer rate limiter; the light-client protocol carries none, allowing a single unprivileged peer to drive unbounded I/O and CPU load on the full node.

## Finding Description
`LightClientProtocol` is defined with only a `shared: Shared` field and no `rate_limiter`: [1](#0-0) 

`try_process` dispatches to handlers with zero rate-limit checks before any handler is invoked: [2](#0-1) 

`GetLastStateProcess::execute()` unconditionally calls `get_verifiable_tip_header()` on every invocation, regardless of the `subscribe` flag: [3](#0-2) 

`get_verifiable_tip_header()` performs three operations per call: (1) `self.shared.snapshot()` — Arc clone of the chain snapshot, (2) `snapshot.get_block(&tip_hash)` — RocksDB read from `COLUMN_BLOCK_BODY`, and (3) `snapshot.chain_root_mmr(tip_block.number() - 1).get_root()` — O(log N) RocksDB reads from `COLUMN_CHAIN_ROOT_MMR`: [4](#0-3) 

`chain_root_mmr` constructs an MMR of size `leaf_index_to_mmr_size(block_number)` backed by the RocksDB snapshot store: [5](#0-4) 

By contrast, `Relayer` carries an explicit `governor`-based rate limiter field: [6](#0-5) 

And checks it before any handler dispatch: [7](#0-6) 

`HolePunching` applies the same pattern: [8](#0-7) 

A grep for `rate_limiter` under `util/light-client-protocol-server/` returns zero matches, confirming no guard exists anywhere in the light-client protocol codebase.

## Impact Explanation
A single unprivileged peer connecting on the `LightClient` protocol can send `GetLastState` messages at the maximum rate the TCP connection allows. Each message unconditionally triggers one RocksDB block-body read and O(log N) RocksDB reads from `COLUMN_CHAIN_ROOT_MMR`, where N is the current chain height. On mainnet with millions of blocks, the MMR peak node count per `get_root()` call is substantial. Sustained, unbounded I/O and CPU amplification from even a single attacking peer can degrade node performance to the point of unresponsiveness or crash, and scales linearly with the number of attacking peers. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** and **"Vulnerabilities which could easily crash a CKB node"** (10001–15000 points).

## Likelihood Explanation
Any peer that can open a TCP connection on the `LightClient` protocol endpoint can trigger this — no authentication, no PoW, no privileged role is required. The `subscribe` flag value is irrelevant; the MMR read occurs on every `GetLastState` message unconditionally. The attack is trivially repeatable and requires no special knowledge.

## Recommendation
Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`, mirroring the pattern in `Relayer`:
```rust
pub struct LightClientProtocol {
    pub shared: Shared,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```
Initialize it with the same 30 req/s per-peer-per-message-type quota used by `Relayer::new()`, and check it at the top of `try_process` before dispatching to any handler, returning `StatusCode::TooManyRequests` on limit breach. Call `rate_limiter.retain_recent()` in the `disconnected` handler to prevent unbounded memory growth.

## Proof of Concept
1. Connect a peer to the node's `LightClient` protocol endpoint (no credentials needed).
2. In a tight loop, send `GetLastState { subscribe: false }` (or `true` — the work performed is identical).
3. Monitor via RocksDB statistics or `perf` that `COLUMN_CHAIN_ROOT_MMR` read IOPS and CPU time scale linearly with message rate.
4. Confirm that no `TooManyRequests` status is ever returned (unlike `Relayer`, which returns it after 30 req/s per peer per message type).
5. Observe node CPU and I/O saturation with a single attacking peer sending at TCP line rate.

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-29)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}
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

**File:** util/light-client-protocol-server/src/components/get_last_state.rs (L29-45)
```rust
    pub(crate) async fn execute(self) -> Status {
        let subscribe: bool = self.message.subscribe().into();
        if subscribe {
            self.nc.with_peer_mut(
                self.peer,
                Box::new(|peer| {
                    peer.if_lightclient_subscribed = true;
                }),
            );
        }

        let tip_header = match self.protocol.get_verifiable_tip_header() {
            Ok(tip_state) => tip_state,
            Err(errmsg) => {
                return StatusCode::InternalError.with_context(errmsg);
            }
        };
```

**File:** util/snapshot/src/lib.rs (L180-184)
```rust
    /// Returns the chain root MMR for a provided block.
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
    }
```

**File:** sync/src/relayer/mod.rs (L78-82)
```rust
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
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

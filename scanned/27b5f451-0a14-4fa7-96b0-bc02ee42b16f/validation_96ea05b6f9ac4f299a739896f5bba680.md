### Title
Missing Per-Peer Rate Limit on `GetLastState` Handler Enables Unbounded MMR Database Reads — (`util/light-client-protocol-server/src/lib.rs`, `util/light-client-protocol-server/src/components/get_last_state.rs`)

---

### Summary

`LightClientProtocol` contains no rate limiter at all. Every `GetLastState` message from any peer unconditionally invokes `get_verifiable_tip_header()`, which performs a RocksDB snapshot acquisition, a block-body read, and an O(log N) MMR root read from `COLUMN_CHAIN_ROOT_MMR`. The Relayer and HolePunching protocols both carry a `governor`-based 30 req/s per-peer rate limiter; the light-client protocol carries none.

---

### Finding Description

`LightClientProtocol` is defined as:

```rust
pub struct LightClientProtocol {
    pub shared: Shared,
}
``` [1](#0-0) 

There is no `rate_limiter` field. The `try_process` dispatch path performs zero rate-limit checks before handing off to any handler:

```rust
async fn try_process(...) -> Status {
    match message {
        packed::LightClientMessageUnionReader::GetLastState(reader) => {
            components::GetLastStateProcess::new(reader, self, peer_index, nc)
                .execute()
                .await
        }
        ...
    }
}
``` [2](#0-1) 

`GetLastStateProcess::execute()` calls `get_verifiable_tip_header()` on every invocation, regardless of the `subscribe` flag value: [3](#0-2) 

`get_verifiable_tip_header()` performs:
1. `self.shared.snapshot()` — acquires an `Arc` clone of the current chain snapshot
2. `snapshot.get_block(&tip_hash)` — RocksDB read from `COLUMN_BLOCK_BODY`
3. `snapshot.chain_root_mmr(tip_block.number() - 1).get_root()` — O(log N) RocksDB reads from `COLUMN_CHAIN_ROOT_MMR` [4](#0-3) 

`chain_root_mmr` constructs an MMR of size `leaf_index_to_mmr_size(block_number)` backed by the RocksDB snapshot store: [5](#0-4) 

The `COLUMN_CHAIN_ROOT_MMR` column family is a dedicated RocksDB column: [6](#0-5) 

**Contrast with the Relayer**, which carries an explicit `governor`-based rate limiter and checks it before any handler dispatch:

```rust
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
``` [7](#0-6) 

```rust
// setup a rate limiter keyed by peer and message type that lets through 30 requests per second
let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
let rate_limiter = RateLimiter::hashmap(quota);
``` [8](#0-7) 

```rust
if should_check_rate && self.rate_limiter.check_key(&(peer, message.item_id())).is_err() {
    return StatusCode::TooManyRequests.with_context(message.item_name());
}
``` [9](#0-8) 

The HolePunching protocol applies the same pattern: [10](#0-9) 

The light-client protocol has no equivalent guard anywhere in its codebase (confirmed: zero occurrences of `rate_limiter` under `util/light-client-protocol-server/`).

---

### Impact Explanation

A single unprivileged peer connecting on the `LightClient` protocol can send `GetLastState` messages at the maximum rate the TCP connection allows. Each message unconditionally triggers:
- One RocksDB block-body read
- O(log N) RocksDB reads from `COLUMN_CHAIN_ROOT_MMR` (where N is the current chain height — on mainnet, millions of blocks)

This produces sustained, unbounded CPU and I/O amplification on the full node proportional to the attacker's message rate, with no per-peer throttle. The impact is proportional to chain height (larger MMR = more peak nodes to read per `get_root()` call) and scales linearly with the number of attacking peers.

---

### Likelihood Explanation

Any peer that can open a connection on the `LightClient` protocol (no authentication required) can trigger this. The `subscribe` flag value is irrelevant to the work performed — the MMR read occurs on every `GetLastState` message unconditionally. The attack requires no special knowledge, no PoW, and no privileged role.

---

### Recommendation

Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol` (mirroring `Relayer::rate_limiter`) and check it at the top of `try_process` before dispatching to any handler, using the same 30 req/s per-peer-per-message-type quota already established in the codebase.

---

### Proof of Concept

1. Connect a peer to the node's `LightClient` protocol endpoint.
2. In a tight loop, send `GetLastState { subscribe: true }` (or `false` — the work is identical).
3. Observe via `perf` or RocksDB statistics that `COLUMN_CHAIN_ROOT_MMR` read IOPS and CPU time scale linearly with message rate.
4. Assert that no `TooManyRequests` status is ever returned (unlike the Relayer, which returns it after 30 req/s per peer).

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

**File:** util/light-client-protocol-server/src/lib.rs (L127-155)
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

        let tip_header = packed::VerifiableHeader::new_builder()
            .header(tip_block.header().data())
            .uncles_hash(tip_block.calc_uncles_hash())
            .extension(Pack::pack(&tip_block.extension()))
            .parent_chain_root(parent_chain_root)
            .build();

        Ok(tip_header)
    }
```

**File:** util/light-client-protocol-server/src/components/get_last_state.rs (L29-55)
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

        let content = packed::SendLastState::new_builder()
            .last_header(tip_header)
            .build();
        let message = packed::LightClientMessage::new_builder()
            .set(content)
            .build();

        self.nc.reply(self.peer, &message).await
    }
```

**File:** util/snapshot/src/lib.rs (L180-184)
```rust
    /// Returns the chain root MMR for a provided block.
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
    }
```

**File:** db-schema/src/lib.rs (L41-42)
```rust
/// Column store chain root MMR data
pub const COLUMN_CHAIN_ROOT_MMR: Col = "16";
```

**File:** sync/src/relayer/mod.rs (L78-82)
```rust
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
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

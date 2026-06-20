## Analysis

**Tracing the attack path:**

1. Any unprivileged P2P peer connects to a node with `LightClientProtocol` enabled.
2. Peer sends `GetLastStateProof` (or `GetBlocksProof`/`GetTransactionsProof`) messages.
3. `LightClientProtocol::received()` dispatches to `GetLastStateProofProcess::execute()`.
4. `complete_headers()` calls `snapshot.chain_root_mmr(*number - 1).get_root()` **for each sampled block number**.
5. `reply_proof()` then calls `mmr.get_root()` AND `mmr.gen_proof()` on the tip MMR.
6. Each `get_root()`/`gen_proof()` call triggers O(log(mmr_size)) calls to `get_elem()` → `store.get_header_digest(pos)` → direct RocksDB lookup.

**Checking for guards:**

- `LightClientProtocol` struct has **no rate limiter field** — compare to `Relayer` which has `rate_limiter: RateLimiter<(PeerIndex, u32)>` and checks it on every message.
- `StoreCache` caches headers, cell data, block proposals, tx hashes, uncles, and extensions — but **no header digests** (MMR nodes). Every `get_elem()` is an uncached RocksDB read.
- `GET_LAST_STATE_PROOF_LIMIT = 1000` limits samples per request, but there is **no per-peer or per-second request frequency limit**.
- With 1000 samples at chain height ~10M blocks: 1000 × O(log₂(2×10⁷)) ≈ 1000 × 25 = **~25,000 RocksDB lookups per request**, with no throttle on request rate.

---

### Title
Missing Rate Limiting in `LightClientProtocol` Enables I/O Exhaustion via Repeated MMR Proof Requests — (`util/light-client-protocol-server/src/lib.rs`, `util/snapshot/src/lib.rs`)

### Summary
The `LightClientProtocol` handler has no per-peer rate limiter. Any unprivileged P2P peer can flood the node with `GetLastStateProof`/`GetBlocksProof` messages, each triggering O(samples × log(chain_height)) uncached RocksDB reads via `MMRStore::get_elem()`, exhausting I/O and degrading node performance.

### Finding Description

`LightClientProtocol` contains only a `shared: Shared` field — no rate limiter: [1](#0-0) 

Every incoming message is dispatched directly without any rate check: [2](#0-1) 

Contrast with `Relayer`, which has a `rate_limiter` and checks it on every message before dispatch: [3](#0-2) 

For `GetLastStateProof`, `complete_headers()` calls `chain_root_mmr(*number - 1).get_root()` for **each** sampled block number (up to `GET_LAST_STATE_PROOF_LIMIT = 1000`): [4](#0-3) 

Then `reply_proof()` calls `mmr.get_root()` and `mmr.gen_proof()` again on the tip MMR: [5](#0-4) 

Each `get_root()`/`gen_proof()` call invokes `get_elem()` O(log(mmr_size)) times: [6](#0-5) 

`get_header_digest()` is a direct RocksDB read — `StoreCache` has **no cache for header digests**: [7](#0-6) [8](#0-7) 

### Impact Explanation

At chain height N ≈ 10M blocks, one `GetLastStateProof` request with 1000 samples triggers approximately 1000 × log₂(2×10⁷) + log₂(2×10⁷) ≈ **25,000+ uncached RocksDB point lookups**. With no rate limiting, an attacker sending requests in a tight loop can saturate disk I/O, causing the node to become unresponsive to legitimate peers and block propagation — directly causing network congestion.

### Likelihood Explanation

The light client protocol is enabled by default in production CKB nodes. Any peer that connects can send these messages without any PoW, stake, or other cost. The attack requires only a standard P2P connection and knowledge of a valid `last_hash` (the chain tip hash, which is publicly broadcast). The absence of a rate limiter (unlike `Relayer`) makes this straightforwardly exploitable.

### Recommendation

Add a per-peer rate limiter to `LightClientProtocol` analogous to the one in `Relayer`:

```rust
pub struct LightClientProtocol {
    pub shared: Shared,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

Check it in `try_process()` before dispatching any message. Additionally, consider adding an application-level LRU cache for `HeaderDigest` entries in `StoreCache` to reduce per-request DB I/O.

### Proof of Concept

1. Connect a peer to a CKB node with a long chain (e.g., mainnet at ~13M blocks).
2. Send repeated `GetLastStateProof` messages with `difficulties.len() = 998` and `last_n_blocks = 1` (total = 1000, at the `GET_LAST_STATE_PROOF_LIMIT`), using the current tip hash as `last_hash`.
3. Measure RocksDB read IOPS via `iostat` — each request generates ~25,000 reads.
4. Observe node CPU/IO saturation and degraded block relay latency. [9](#0-8)

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-36)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}

impl LightClientProtocol {
    /// Create a new light client protocol handler.
    pub fn new(shared: Shared) -> Self {
        Self { shared }
    }
}
```

**File:** util/light-client-protocol-server/src/lib.rs (L55-92)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        data: Bytes,
    ) {
        trace!("LightClient.received peer={}", peer);

        let msg = match packed::LightClientMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "LightClient.received a malformed message from Peer({})",
                    peer
                );
                nc.ban_peer(
                    peer,
                    constant::BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();
        let status = self.try_process(&nc, peer, msg).await;
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn!("process {} from {}; result is {}", item_name, peer, status);
        } else if !status.is_ok() {
            debug!("process {} from {}; result is {}", item_name, peer, status);
        }
    }
```

**File:** util/light-client-protocol-server/src/lib.rs (L195-218)
```rust
        let (parent_chain_root, proof) = if last_block.is_genesis() {
            (Default::default(), Default::default())
        } else {
            let snapshot = self.shared.snapshot();
            let mmr = snapshot.chain_root_mmr(last_block.number() - 1);
            let parent_chain_root = match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return StatusCode::InternalError.with_context(errmsg);
                }
            };
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                }
            };
            (parent_chain_root, proof)
```

**File:** sync/src/relayer/mod.rs (L78-123)
```rust
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
    }

    /// Get shared state
    pub fn shared(&self) -> &Arc<SyncShared> {
        &self.shared
    }

    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::RelayMessageUnionReader<'_>,
    ) -> Status {
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L150-163)
```rust
                let parent_chain_root = if *number == 0 {
                    Default::default()
                } else {
                    let mmr = self.snapshot.chain_root_mmr(*number - 1);
                    match mmr.get_root() {
                        Ok(root) => root,
                        Err(err) => {
                            let errmsg = format!(
                                "failed to generate a root for block#{number} since {err:?}"
                            );
                            return Err(errmsg);
                        }
                    }
                };
```

**File:** util/snapshot/src/lib.rs (L293-296)
```rust
impl MMRStore<HeaderDigest> for &Snapshot {
    fn get_elem(&self, pos: u64) -> MMRResult<Option<HeaderDigest>> {
        Ok(self.store.get_header_digest(pos))
    }
```

**File:** store/src/store.rs (L554-561)
```rust
    fn get_header_digest(&self, position_u64: u64) -> Option<packed::HeaderDigest> {
        let position: packed::Uint64 = position_u64.into();
        self.get(COLUMN_CHAIN_ROOT_MMR, position.as_slice())
            .map(|slice| {
                let reader = packed::HeaderDigestReader::from_slice_should_be_ok(slice.as_ref());
                reader.to_entity()
            })
    }
```

**File:** store/src/cache.rs (L11-26)
```rust
pub struct StoreCache {
    /// The cache of block headers
    pub headers: Mutex<LruCache<Byte32, HeaderView>>,
    /// The cache of cell data.
    pub cell_data: Mutex<LruCache<Vec<u8>, (Bytes, Byte32)>>,
    /// The cache of cell data hash.
    pub cell_data_hash: Mutex<LruCache<Vec<u8>, Byte32>>,
    /// The cache of block proposals.
    pub block_proposals: Mutex<LruCache<Byte32, ProposalShortIdVec>>,
    /// The cache of block transaction hashes.
    pub block_tx_hashes: Mutex<LruCache<Byte32, Vec<Byte32>>>,
    /// The cache of block uncles.
    pub block_uncles: Mutex<LruCache<Byte32, UncleBlockVecView>>,
    /// The cache of block extension sections.
    pub block_extensions: Mutex<LruCache<Byte32, Option<packed::Bytes>>>,
}
```

**File:** util/light-client-protocol-server/src/constant.rs (L1-7)
```rust
use std::time::Duration;

pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);

pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

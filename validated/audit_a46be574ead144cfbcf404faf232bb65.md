Looking at the full call chain and comparing `LightClientProtocol` against other protocol handlers that do have rate limiting.

### Title
Missing Rate Limiter in `LightClientProtocol` Enables Unbounded MMR DB I/O via Repeated Proof Requests — (`util/light-client-protocol-server/src/lib.rs`, `util/snapshot/src/lib.rs`)

---

### Summary

The `LightClientProtocol` handler has no per-peer rate limiter, unlike `Relayer` and `HolePunching` which both enforce 30 req/sec. Any unauthenticated peer can flood the node with `GetLastStateProof` or `GetBlocksProof` messages. Each request triggers O(samples × log(mmr_size)) direct RocksDB lookups through `get_elem()` → `get_header_digest()`, with no caching and no request throttle. On a long chain this causes I/O exhaustion and node unresponsiveness.

---

### Finding Description

**Root cause — no rate limiter in `LightClientProtocol`:**

The `LightClientProtocol` struct contains only a `shared: Shared` field with no rate limiter: [1](#0-0) 

Its `received()` handler dispatches directly to `try_process()` with no rate check: [2](#0-1) 

By contrast, `Relayer` enforces 30 req/sec per peer per message type before any processing: [3](#0-2) 

And `HolePunching` similarly has both a `rate_limiter` and `forward_rate_limiter`: [4](#0-3) 

**Attack path — `GetLastStateProof` with 1000 samples:**

`GetLastStateProofProcess::execute()` only checks that `difficulties.len() + last_n_blocks * 2 ≤ 1000` (a per-request payload cap, not a request-rate cap): [5](#0-4) 

It then calls `complete_headers()`, which calls `snapshot.chain_root_mmr(*number - 1).get_root()` for **each** sampled block number — up to 1000 iterations: [6](#0-5) 

After that, `reply_proof()` calls `mmr.get_root()` **and** `mmr.gen_proof(items_positions)` — two additional MMR traversals: [7](#0-6) 

**Each MMR traversal is an unbuffered RocksDB lookup chain:**

`chain_root_mmr()` creates a new `ChainRootMMR` with `mmr_size = leaf_index_to_mmr_size(block_number)`: [8](#0-7) 

Every `get_elem()` call on `&Snapshot` is a direct, uncached RocksDB point lookup: [9](#0-8) 

Which resolves to `get_header_digest()` — a raw `COLUMN_CHAIN_ROOT_MMR` read: [10](#0-9) 

**`GetLastState` is also unguarded:**

Even the cheapest message, `GetLastState`, calls `get_verifiable_tip_header()` which performs `chain_root_mmr(tip_number - 1).get_root()` — O(log N) DB lookups — on every invocation with no rate limit: [11](#0-10) 

---

### Impact Explanation

On a chain with N blocks, `leaf_index_to_mmr_size(N)` ≈ 2N MMR nodes. `get_root()` fetches O(log N) peak nodes; `gen_proof(k positions)` fetches O(k × log N) nodes. `complete_headers()` with 1000 sampled blocks calls `get_root()` 1000 times. A single `GetLastStateProof` request at chain height ~14M (CKB mainnet scale) triggers roughly:

- `complete_headers`: 1000 × ~25 = **25,000 DB reads**
- `reply_proof` `get_root` + `gen_proof(1000)`: ~25,025 DB reads
- **Total: ~50,000 RocksDB point lookups per request**

With no rate limiter, an attacker with a single TCP connection can saturate the node's I/O subsystem, starving block processing, sync, and transaction relay. The `GET_LAST_STATE_PROOF_LIMIT = 1000` constant only caps payload size per message, not message frequency. [12](#0-11) 

---

### Likelihood Explanation

The light client protocol (`/ckb/lightclient`, protocol ID 120) is a standard supported protocol. Any peer that connects and negotiates it can send these messages without any authentication, PoW, or stake. The attack requires only a TCP connection and knowledge of the molecule-encoded message format, which is public. The attacker can open multiple connections to multiply the effect.

---

### Recommendation

Add a per-peer, per-message-type rate limiter to `LightClientProtocol` matching the pattern used in `Relayer` (30 req/sec per peer per message type). Additionally, consider caching the MMR root for the current tip (it is recomputed identically on every `GetLastState` and `GetBlocksProof` response) and limiting the number of concurrent in-flight MMR proof computations.

---

### Proof of Concept

1. Connect to a CKB full node with the light client protocol enabled on a long chain.
2. Send repeated `GetLastStateProof` messages with `last_n_blocks = 500` and `difficulties` containing 500 entries (total = 1000, at the limit).
3. Set `last_hash` to the current tip hash (always valid).
4. Observe via `iostat` or RocksDB metrics that each message triggers ~50,000 DB reads.
5. At 100 messages/sec (easily achievable over a single TCP connection), this produces ~5,000,000 DB reads/sec, saturating typical NVMe I/O and causing the node to stop processing new blocks and peer messages.

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

**File:** sync/src/relayer/mod.rs (L88-123)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L38-47)
```rust
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L198-205)
```rust
    pub(crate) async fn execute(self) -> Status {
        let last_n_blocks: u64 = self.message.last_n_blocks().into();

        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
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

**File:** util/light-client-protocol-server/src/constant.rs (L1-7)
```rust
use std::time::Duration;

pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);

pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

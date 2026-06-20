Now I have all the information needed for a complete analysis. Let me trace every step of the claimed path.

**Execution trace for the attack scenario:**

1. `LightClientProtocol::received()` — no rate limiter exists here (unlike `Relayer` which has one at lines 91-92 of `relayer/mod.rs`)
2. `execute()` in `get_blocks_proof.rs`:
   - Line 34-36: `block_hashes.is_empty()` → false, passes
   - Line 38-40: `len() > GET_BLOCKS_PROOF_LIMIT` (1000) → exactly 1000, passes
   - Line 45: `is_main_chain(&last_block_hash)` → true (attacker uses valid tip hash), passes
   - Line 63-70: dedup check → passes (all 1000 unique)
   - Line 72-74: partition → all 1000 go to `missing`, `found` is empty
   - Line 81-95: loop over `found` → empty, zero work
   - Line 104-113: calls `reply_proof()` with empty `positions`
3. `reply_proof()` (lib.rs lines 195-218):
   - `last_block.is_genesis()` → false (chain height ≥ 1)
   - Takes a second snapshot
   - Calls `snapshot.chain_root_mmr(last_block.number() - 1).get_root()` — O(log N) DB reads
   - `items_positions.is_empty()` → true, proof is `Default::default()`
   - Returns `Status::ok()`
4. Back in `received()`: `status.should_ban()` checks `if !(400..500).contains(&code)` → code is 200, returns `None` → **no ban**

**The `should_ban()` logic confirms no ban is possible for this path:** [1](#0-0) 

**There is no rate limiter in `LightClientProtocol`:** [2](#0-1) 

Compare with `Relayer`, which has an explicit per-peer rate limiter: [3](#0-2) 

**The MMR root computation is a real DB cost:** [4](#0-3) [5](#0-4) 

Each `get_root()` call traverses O(log N) MMR nodes via `get_header_digest()` DB reads. The 1000 `is_main_chain()` calls for random hashes are additional DB lookups that will miss any cache.

**The limit is 1000 per message:** [6](#0-5) 

The test at line 78 of the test file even explicitly asserts `nc.not_banned(peer_index)` for the all-missing case, confirming this is the intended (but unprotected) behavior: [7](#0-6) 

---

### Title
Unbounded MMR Root Computation with No Rate Limiting or Peer Ban on All-Missing `GetBlocksProof` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

### Summary
An unprivileged remote peer can repeatedly send well-formed `GetBlocksProof` messages with a valid `last_hash` (current tip) and up to 1000 non-main-chain block hashes. Each message triggers 1000 `is_main_chain()` DB lookups, a second snapshot acquisition, and an MMR root computation (`chain_root_mmr(...).get_root()`). The server returns `Status::ok()`, which never triggers a ban. There is no rate limiter on the `LightClientProtocol` handler. An attacker can sustain this at full network speed indefinitely.

### Finding Description
In `GetBlocksProofProcess::execute()`, after the `last_hash` is validated as on-chain, all `block_hashes` are partitioned into `found`/`missing` via `is_main_chain()`. When all hashes are missing, `reply_proof()` is called with empty `positions`. Inside `reply_proof()`, for any non-genesis `last_block`, the code unconditionally calls `snapshot.chain_root_mmr(last_block.number() - 1).get_root()` regardless of whether any blocks were actually found. This returns `Status::ok()`, which maps to HTTP-200 and is explicitly excluded from the ban path (`should_ban()` only fires for 4xx codes). The `LightClientProtocol::received()` handler has no rate limiter (unlike `Relayer` and `HolePunching` which both have per-peer `governor::RateLimiter` instances).

### Impact Explanation
Each malicious message costs the server: 1000 RocksDB point-lookups for `is_main_chain()` (all cache-missing for random hashes), one additional DB snapshot acquisition, and O(log N) RocksDB reads for the MMR root traversal (where N = chain height). At mainnet heights (millions of blocks), log₂(N) ≈ 20+ DB reads per MMR root. A single attacker sending messages at network speed can saturate the server's I/O and CPU, degrading or denying service to legitimate light clients.

### Likelihood Explanation
The attack requires only a valid P2P connection to a light-client-serving node and knowledge of the current tip hash (publicly available). No PoW, no keys, no special privileges. The attacker constructs random 32-byte hashes as `block_hashes`. The path is reachable on mainnet today.

### Recommendation
1. Add a per-peer rate limiter to `LightClientProtocol` (mirroring the `governor::RateLimiter` pattern used in `Relayer`).
2. Consider returning a ban-eligible status (4xx) or simply dropping the response when all requested blocks are missing, rather than performing MMR root computation for zero-result queries.
3. Alternatively, skip the `get_root()` call entirely when `positions` is empty and return a lightweight response.

### Proof of Concept
```rust
// Attacker sends repeatedly:
let content = packed::GetBlocksProof::new_builder()
    .last_hash(/* current tip hash, obtained from GetLastState */)
    .block_hashes(
        (0u64..1000)
            .map(|i| {
                let mut h = [0u8; 32];
                h[..8].copy_from_slice(&i.to_le_bytes());
                packed::Byte32::from_slice(&h).unwrap()
            })
            .collect::<Vec<_>>()
    )
    .build();
// Server performs: 1000x is_main_chain() + MMR get_root() per message, no ban, no rate limit.
```

### Citations

**File:** util/light-client-protocol-server/src/status.rs (L95-102)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
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

**File:** sync/src/relayer/mod.rs (L89-123)
```rust
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

**File:** util/snapshot/src/lib.rs (L181-184)
```rust
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

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/tests/components/get_blocks_proof.rs (L73-78)
```rust
    assert!(nc.sent_messages().borrow().is_empty());

    let peer_index = PeerIndex::new(1);
    protocol.received(nc.context(), peer_index, data).await;

    assert!(nc.not_banned(peer_index));
```

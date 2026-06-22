Now I have all the information needed. Let me verify the exact limit arithmetic and the absence of rate limiting in the light client protocol.

### Title
Unbounded MMR DB Read Amplification via `GetLastStateProof` with `last_n_blocks=500` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unprivileged light-client peer can send a crafted but structurally valid `GetLastStateProof` message that causes the server to perform 500 separate `chain_root_mmr().get_root()` DB read operations per request, with no per-peer rate limiting in the light-client protocol handler. The peer is never banned for sending such requests, enabling sustained CPU/IO amplification.

---

### Finding Description

**Limit check passes at exactly the boundary.**

`GET_LAST_STATE_PROOF_LIMIT = 1000`. The guard in `execute()` is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

With `difficulties = []` and `last_n_blocks = 500`: `0 + 500×2 = 1000`, which is **not** `> 1000`. The check passes. [1](#0-0) [2](#0-1) 

**500 blocks are collected for `complete_headers`.**

With `start_block_number = last_block_number - 500` and `last_n_blocks = 500`, the condition `last_block_number - start_block_number <= last_n_blocks` evaluates to `500 <= 500 = true`, so the "not enough blocks" branch fires and `last_n_numbers` collects all 500 block numbers. [3](#0-2) 

**`complete_headers` calls `chain_root_mmr().get_root()` once per block.**

For each of the 500 block numbers, a fresh `ChainRootMMR` is constructed and `get_root()` is called. Each `get_root()` reads all MMR peak nodes from RocksDB — O(log N) reads for a chain of height N (≈20–30 reads on mainnet). [4](#0-3) [5](#0-4) 

**No rate limiter exists in `LightClientProtocol`.**

A `grep` for `rate_limiter` across the entire `util/light-client-protocol-server/` tree returns zero matches. Compare this to `sync/src/relayer/mod.rs`, which explicitly constructs a `governor::RateLimiter` keyed by `(peer, message_type)` and checks it before every message dispatch. [6](#0-5) [7](#0-6) 

**A valid request never triggers a ban.**

`BAD_MESSAGE_BAN_TIME` (5 minutes) is only applied when the message fails to parse. A well-formed `GetLastStateProof` with `last_n_blocks=500` returns `Status::ok()`, so `should_ban()` is false and the peer is never disconnected or penalized. [8](#0-7) [9](#0-8) 

**Additional work per request.**

Beyond the 500 `get_root()` calls, `complete_headers` also calls `snapshot.get_ancestor()` and `snapshot.get_block()` for each of the 500 blocks, and `reply_proof` calls `mmr.gen_proof(500 positions)` — an O(N log N) proof generation step. [10](#0-9) [11](#0-10) 

---

### Impact Explanation

A single attacker peer can continuously send max-cost `GetLastStateProof` messages at the TCP connection's throughput limit. Each message triggers ~500 RocksDB peak reads (×O(log N) each), 500 ancestor/block lookups, and one large MMR proof generation — all on the server's I/O and CPU. With no rate limiting and no ban, the attacker is never throttled. Multiple attacker IPs multiply the effect linearly. This can saturate the node's I/O subsystem and degrade or halt service for all other peers (sync, relay, legitimate light clients).

---

### Likelihood Explanation

The attack requires only a valid P2P connection to the light-client protocol endpoint, which is an unprivileged role. The crafted message is structurally valid and passes all existing guards. No PoW, no key, no special privilege is needed. The attacker needs to know one valid main-chain block hash (trivially obtained from any public explorer or by first sending `GetLastState`).

---

### Recommendation

1. **Add a per-peer rate limiter** to `LightClientProtocol`, mirroring the `governor::RateLimiter` already used in `Relayer`. Key it by `(PeerIndex, message_item_id)`.
2. **Fix the off-by-one in the limit check**: change `>` to `>=` so that `last_n_blocks=500` with empty difficulties is rejected (`1000 >= 1000`).
3. **Batch or cache MMR root reads**: instead of calling `chain_root_mmr(n).get_root()` independently for each block in `complete_headers`, compute roots in a single MMR traversal or cache the peak nodes across calls within the same request.

---

### Proof of Concept

```
Preconditions:
  - Server chain height >= 500
  - Attacker connects as a light-client peer
  - tip_hash = any valid main-chain tip hash (from GetLastState)

Message fields:
  last_hash          = tip_hash
  start_hash         = hash of block (tip_number - 500)
  start_number       = tip_number - 500
  last_n_blocks      = 500
  difficulty_boundary = U256::MAX   (or any value; irrelevant for this branch)
  difficulties       = []

Limit check: 0 + 500*2 = 1000, NOT > 1000 → passes
Branch taken: last_block_number - start_block_number = 500 <= 500 → "not enough blocks" path
Result: complete_headers called with 500 block numbers
        → 500 × chain_root_mmr(n-1).get_root() DB reads
        → 500 × get_ancestor() + get_block() lookups
        → mmr.gen_proof(500 positions)

Repeat in a tight loop. No ban, no rate limit.
```

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L3-3)
```rust
pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-148)
```rust
        for number in numbers {
            if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
                let position = leaf_index_to_pos(*number);
                positions.push(position);

                let ancestor_block = self
                    .snapshot
                    .get_block(&ancestor_header.hash())
                    .ok_or_else(|| {
                        format!(
                            "failed to find block for header#{} (hash: {:#x})",
                            number,
                            ancestor_header.hash()
                        )
                    })?;
                let uncles_hash = ancestor_block.calc_uncles_hash();
                let extension = ancestor_block.extension();
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L199-205)
```rust
        let last_n_blocks: u64 = self.message.last_n_blocks().into();

        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-297)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
            (sampled_numbers, last_n_numbers)
```

**File:** util/snapshot/src/lib.rs (L181-184)
```rust
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
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

**File:** util/light-client-protocol-server/src/lib.rs (L207-217)
```rust
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

Looking at the actual code, I need to verify: (1) whether `LightClientProtocol` has rate limiting, (2) what work `reply_proof` does with the positions, and (3) what the actual limit is.

All the key evidence is now in hand. Here is my analysis:

**Rate limiting in `LightClientProtocol`:**

The struct definition is:
```rust
pub struct LightClientProtocol {
    shared: Shared,
}
``` [1](#0-0) 

There is **no rate limiter field**. The `received` handler dispatches directly to `try_process` with zero per-peer throttling. [2](#0-1) 

Compare this to `Relayer`, which has an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` and checks it before every message: [3](#0-2) 

And `HolePunching`, which has both `rate_limiter` and `forward_rate_limiter`: [4](#0-3) 

**The limit is 1000, not a small number:**

`GET_TRANSACTIONS_PROOF_LIMIT = 1000` [5](#0-4) 

**Work done per request with 1000 distinct tx hashes in 1000 distinct blocks:**

1. 1000 × `get_transaction_info` (DB read)
2. 1000 × `get_transaction_with_info` (DB read)
3. 1000 × `get_block` (full block fetch from DB)
4. 1000 × `CBMT::build_merkle_proof` (per-block Merkle proof)
5. 1000 × `get_block_uncles` + `get_block_extension`
6. `mmr.gen_proof(1000 positions)` — one call with all 1000 spread positions [6](#0-5) 

The `reply_proof` function calls `mmr.gen_proof(items_positions)` with the full vector: [7](#0-6) 

---

### Title
Unbounded per-peer request rate in `LightClientProtocol` enables CPU/IO exhaustion via `GetTransactionsProof` — (`util/light-client-protocol-server/src/lib.rs`, `src/components/get_transactions_proof.rs`)

### Summary
`LightClientProtocol` has no per-peer rate limiter. Any connected peer can repeatedly send `GetTransactionsProof` messages with up to 1000 tx hashes spread across 1000 distinct blocks, triggering 1000+ RocksDB reads, 1000 CBMT proof builds, and a single `mmr.gen_proof(1000 positions)` call per message, with no throttle to bound the aggregate server-side work.

### Finding Description
The `LightClientProtocol` struct contains only a `shared: Shared` field — no rate limiter. [1](#0-0) 

The `received` handler parses the message and immediately calls `try_process` without any per-peer quota check. [2](#0-1) 

The only guard inside `execute` is a count check against `GET_TRANSACTIONS_PROOF_LIMIT = 1000`, which is the *maximum* allowed, not a throttle. [8](#0-7) 

When 1000 tx hashes each belonging to a distinct block are submitted, the handler:
- Fetches 1000 full blocks from RocksDB
- Builds 1000 CBMT Merkle proofs
- Calls `mmr.gen_proof` with 1000 spread leaf positions against the full chain MMR [9](#0-8) [7](#0-6) 

This is in direct contrast to `Relayer` and `HolePunching`, which both carry a `RateLimiter` and reject excess requests before doing any work. [10](#0-9) 

### Impact Explanation
A single attacker peer can sustain a stream of max-size `GetTransactionsProof` messages. Each message forces the server to perform ~3000 RocksDB reads (tx info, full block, uncles/extension per block) plus one `mmr.gen_proof` call over 1000 spread positions. On a long chain this is O(1000 · log N) MMR node visits. With no rate limit, the attacker can saturate the server's I/O and CPU threads, degrading or halting service for legitimate light clients and potentially starving other protocol handlers sharing the same async runtime.

### Likelihood Explanation
The attacker needs only a standard P2P connection to a node with the light client protocol enabled, plus knowledge of 1000 confirmed tx hashes in distinct blocks — all freely available from public chain explorers or by syncing the chain. No key material, PoW, or privileged access is required. The attack is trivially scriptable and repeatable.

### Recommendation
Add a per-peer rate limiter to `LightClientProtocol` following the same pattern used by `Relayer` and `HolePunching`:
- Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`.
- In `received`, check the limiter keyed on `(peer, msg.item_id())` before calling `try_process`, returning early (without banning) if the quota is exceeded.
- Additionally, consider reducing `GET_TRANSACTIONS_PROOF_LIMIT` or adding a secondary limit on the number of *distinct blocks* referenced in a single request, since the per-block work (full block fetch + CBMT proof) is the dominant cost.

### Proof of Concept
```
1. Connect to a CKB full node with light client protocol enabled.
2. Collect 1000 confirmed tx hashes from 1000 distinct blocks (public chain data).
3. In a tight loop, send GetTransactionsProof{last_hash=tip, tx_hashes=[h_0..h_999]}.
4. Observe: each message triggers ~3000 RocksDB reads + mmr.gen_proof(1000 positions).
5. With no rate limiter, the server processes every message; CPU and I/O utilization
   climb proportionally to the request rate.
```

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L31-36)
```rust
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

**File:** sync/src/relayer/mod.rs (L84-123)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L44-47)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L54-126)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = self
            .message
            .tx_hashes()
            .to_entity()
            .into_iter()
            .partition(|tx_hash| {
                snapshot
                    .get_transaction_info(tx_hash)
                    .map(|tx_info| snapshot.is_main_chain(&tx_info.block_hash))
                    .unwrap_or_default()
            });

        let mut txs_in_blocks = HashMap::new();
        for tx_hash in found {
            let (tx, tx_info) = snapshot
                .get_transaction_with_info(&tx_hash)
                .expect("tx exists");
            txs_in_blocks
                .entry(tx_info.block_hash)
                .or_insert_with(Vec::new)
                .push((tx, tx_info.index));
        }

        let mut positions = Vec::with_capacity(txs_in_blocks.len());
        let mut filtered_blocks = Vec::with_capacity(txs_in_blocks.len());
        let mut uncles_hash = Vec::with_capacity(txs_in_blocks.len());
        let mut extensions = Vec::with_capacity(txs_in_blocks.len());

        for (block_hash, txs_and_tx_indices) in txs_in_blocks.into_iter() {
            let block = snapshot
                .get_block(&block_hash)
                .expect("block should be in store");
            let merkle_proof = CBMT::build_merkle_proof(
                &block
                    .transactions()
                    .iter()
                    .map(|tx| tx.hash())
                    .collect::<Vec<_>>(),
                &txs_and_tx_indices
                    .iter()
                    .map(|(_, index)| *index as u32)
                    .collect::<Vec<_>>(),
            )
            .expect("build proof with verified inputs should be OK");

            let txs: Vec<_> = txs_and_tx_indices
                .into_iter()
                .map(|(tx, _)| tx.data())
                .collect();

            let filtered_block = packed::FilteredBlock::new_builder()
                .header(block.header().data())
                .witnesses_root(block.calc_witnesses_root())
                .transactions(txs)
                .proof(
                    packed::MerkleProof::new_builder()
                        .indices(merkle_proof.indices().as_ref())
                        .lemmas(merkle_proof.lemmas().to_owned())
                        .build(),
                )
                .build();

            positions.push(leaf_index_to_pos(block.number()));
            filtered_blocks.push(filtered_block);

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
```

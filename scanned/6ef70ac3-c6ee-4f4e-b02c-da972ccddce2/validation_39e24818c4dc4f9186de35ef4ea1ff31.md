Now I have all the information needed to make a determination. Let me verify the absence of rate limiting in the light client protocol vs. the relay protocol.

### Title
Unbounded Per-Message DB Amplification in `GetTransactionsProof` Handler with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

---

### Summary

An unprivileged remote peer can send a single `GetTransactionsProof` P2P message with 1000 `tx_hashes` (each confirmed in a distinct block) and force the server to perform O(1000 × log(chain_length)) RocksDB reads with no ban triggered and no rate limiting applied.

---

### Finding Description

`GET_TRANSACTIONS_PROOF_LIMIT` is set to 1000: [1](#0-0) 

`GetTransactionsProofProcess::execute` rejects only if `len > 1000`, so exactly 1000 hashes pass through: [2](#0-1) 

For each of the 1000 hashes, the partition step calls `get_transaction_info()`: [3](#0-2) 

Then for each found tx, `get_transaction_with_info()` is called: [4](#0-3) 

Then for each unique block (up to 1000), `get_block()`, `CBMT::build_merkle_proof()`, `get_block_uncles()`, and `get_block_extension()` are all called: [5](#0-4) 

Finally, `reply_proof()` calls `mmr.get_root()` and `mmr.gen_proof(positions)` where `positions` can hold up to 1000 entries. On a chain of length N, `gen_proof` over k positions costs O(k × log N) MMR node reads: [6](#0-5) 

On successful completion the handler returns `Status::ok()` (code 200). `should_ban()` only fires on 4xx codes, so no ban is ever triggered: [7](#0-6) 

Critically, the `LightClientProtocol` struct carries **no rate limiter** — unlike the relay protocol which has an explicit per-peer `RateLimiter`: [8](#0-7) 

The `received()` handler dispatches directly to `try_process()` with no throttle: [9](#0-8) 

---

### Impact Explanation

A single message with 1000 tx_hashes spread across 1000 blocks forces:
- 1000 `get_transaction_info()` DB reads
- 1000 `get_transaction_with_info()` DB reads
- 1000 `get_block()` DB reads
- 1000 `CBMT::build_merkle_proof()` computations
- 1000 `get_block_uncles()` DB reads
- 1000 `get_block_extension()` DB reads
- 1 `mmr.get_root()` + 1 `mmr.gen_proof(1000 positions)` = O(1000 × log(chain_length)) MMR node reads

On a 1M-block chain, `log₂(1M) ≈ 20`, so a single message triggers ~20,000 MMR node reads on top of ~6,000 direct DB reads. With no rate limiting, an attacker can flood the server with these messages continuously, saturating RocksDB I/O and degrading node performance for all peers.

---

### Likelihood Explanation

The attack requires only:
1. A P2P connection to a node running the light client protocol (no authentication)
2. Knowledge of 1000 valid on-chain tx hashes in distinct blocks (trivially obtained by scanning the public chain)
3. The current tip hash as `last_hash` (publicly observable)

No PoW, no key, no privileged role is needed.

---

### Recommendation

1. **Add per-peer rate limiting** to `LightClientProtocol::received()`, mirroring the `RateLimiter` used in the relay protocol (`sync/src/relayer/mod.rs` lines 91–92).
2. **Reduce `GET_TRANSACTIONS_PROOF_LIMIT`** to a value that bounds worst-case DB work to an acceptable level (e.g., 100–200).
3. **Consider banning peers** that repeatedly send maximum-size requests in rapid succession (track request rate per peer).

---

### Proof of Concept

```
1. Run a CKB full node with light client protocol enabled on a chain with ≥1000 blocks.
2. Collect 1000 tx hashes, one from each of 1000 distinct blocks (e.g., cellbase txs).
3. Obtain the current tip hash.
4. Construct a GetTransactionsProof message:
     last_hash  = <tip_hash>
     tx_hashes  = [hash_0, hash_1, ..., hash_999]   // 1000 entries, 1000 distinct blocks
5. Send the message repeatedly in a tight loop from a single peer connection.
6. Monitor RocksDB read counters (rocksdb.number.db.get / iterator ops) — each message
   produces O(6000 + 1000×log(chain_len)) reads with no ban and no throttle.
7. Observe node CPU/IO saturation and degraded response times for other peers.
```

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L54-64)
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
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L67-75)
```rust
        for tx_hash in found {
            let (tx, tx_info) = snapshot
                .get_transaction_with_info(&tx_hash)
                .expect("tx exists");
            txs_in_blocks
                .entry(tx_info.block_hash)
                .or_insert_with(Vec::new)
                .push((tx, tx_info.index));
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L82-126)
```rust
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

**File:** util/light-client-protocol-server/src/lib.rs (L199-216)
```rust
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
```

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

The code confirms this is a real vulnerability. Here is the complete analysis:

---

### Title
Unbounded O(N) Full-Block DB Amplification in `GetTransactionsProofProcess::execute` with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary

An unprivileged remote peer can send a single `GetTransactionsProof` message containing up to `GET_TRANSACTIONS_PROOF_LIMIT` (1000) transaction hashes, each confirmed in a distinct block. The server performs one full block read, one CBMT proof build, one uncle read, and one extension read per unique block, then generates an MMR proof over all 1000 positions — all with no per-peer rate limiting and no ban on success.

### Finding Description

`GET_TRANSACTIONS_PROOF_LIMIT` is set to 1000: [1](#0-0) 

The `execute()` function rejects only messages with `> 1000` hashes, so exactly 1000 is accepted: [2](#0-1) 

For each unique block containing a found transaction, the server calls `snapshot.get_block()` (full block deserialization), `CBMT::build_merkle_proof()`, `snapshot.get_block_uncles()`, and `snapshot.get_block_extension()`: [3](#0-2) 

Then `reply_proof` calls `mmr.gen_proof(items_positions)` with up to 1000 positions (O(N log N) MMR work): [4](#0-3) 

A valid request returns `Status::ok()`, which triggers **no ban**: [5](#0-4) 

`should_ban()` only fires for 4xx status codes. A well-formed 1000-tx request never produces a 4xx: [6](#0-5) 

**Critical contrast**: The `Relayer` protocol has an explicit per-peer, per-message-type rate limiter (`governor::RateLimiter`) checked before every message dispatch: [7](#0-6) 

`LightClientProtocol` has no such field and no rate-limiting check anywhere in `try_process` or `received`. The `HolePunching` protocol also has its own rate limiter. The light client server is the only production protocol handler without one. [8](#0-7) 

Additionally, unlike `GetBlocksProofProcess` which deduplicates block hashes before processing, `GetTransactionsProofProcess` has no deduplication guard on `tx_hashes`. The `txs_in_blocks` HashMap naturally deduplicates by block hash, but 1000 distinct tx_hashes from 1000 distinct blocks is the worst case and is trivially constructable from public chain data. [9](#0-8) 

### Impact Explanation

Each maximally-crafted `GetTransactionsProof` message causes:
- 1000 `snapshot.get_block()` calls (full block deserialization from RocksDB, including all transactions)
- 1000 `CBMT::build_merkle_proof()` computations
- 1000 `snapshot.get_block_uncles()` calls
- 1000 `snapshot.get_block_extension()` calls
- 1 `mmr.gen_proof(1000 positions)` call (O(N log N))
- A large serialized response (1000 `FilteredBlock` entries + MMR proof items)

A single attacker sending these messages in a tight loop can saturate the node's RocksDB I/O, CPU, and outbound bandwidth, degrading or crashing the light client service. No PoW, no stake, no privileged role is required — only a P2P connection to the light client protocol port.

### Likelihood Explanation

The preconditions are trivially met on any mainnet or long-running testnet node:
1. The attacker connects as a light client peer (open P2P endpoint).
2. They obtain 1000 tx hashes from 1000 different blocks by querying any public block explorer or their own synced node.
3. They obtain a valid `last_hash` (any recent tip hash, publicly available).
4. They send the message repeatedly. No ban, no rate limit, no reconnect penalty.

### Recommendation

1. Add a per-peer rate limiter to `LightClientProtocol` mirroring the `governor::RateLimiter` pattern already used in `Relayer` and `HolePunching`.
2. Add a per-request cap on the number of **distinct blocks** (not just tx hashes) that will be processed, e.g., limit `txs_in_blocks.len()` to a value much smaller than 1000 (e.g., 50–100).
3. Add duplicate tx_hash deduplication (as `GetBlocksProofProcess` does for block hashes) to prevent trivial amplification via repeated hashes.

### Proof of Concept

```
1. Attacker connects to a CKB full node's light client P2P port.
2. Attacker collects tx_hash[0..999] where each tx_hash[i] is confirmed
   in a distinct block block[i] (obtainable from any block explorer).
3. Attacker sends:
     GetTransactionsProof {
       last_hash: <any valid main-chain block hash>,
       tx_hashes: [tx_hash[0], tx_hash[1], ..., tx_hash[999]]
     }
4. Server executes GetTransactionsProofProcess::execute():
   - Partitions all 1000 as "found"
   - txs_in_blocks has 1000 entries (one per block)
   - Loop: 1000 × (get_block + CBMT::build_merkle_proof + get_block_uncles + get_block_extension)
   - reply_proof: mmr.gen_proof(1000 positions)
   - Returns Status::ok() — no ban
5. Attacker repeats step 3 in a tight loop.
6. Node's RocksDB I/O and CPU are saturated; light client service degrades.
```

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L54-75)
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

**File:** util/light-client-protocol-server/src/lib.rs (L81-91)
```rust
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

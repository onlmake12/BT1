### Title
Unbounded Per-Block Work in `GetTransactionsProofProcess::execute` Enables DoS via Max-Distinct-Block Request — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

---

### Summary

The `GET_TRANSACTIONS_PROOF_LIMIT` check bounds only the number of `tx_hashes` in a request, not the number of distinct blocks that must be loaded. An unprivileged peer can send 1000 tx_hashes each confirmed in a different block, forcing the server to perform 1000 full block loads, 1000 CBMT proof builds, 1000 witnesses-root computations, 1000 uncle fetches, 1000 extension fetches, and a 1000-position MMR proof — all in a single unauthenticated request with no ban.

---

### Finding Description

The limit check in `execute`: [1](#0-0) 

rejects requests with more than `GET_TRANSACTIONS_PROOF_LIMIT = 1000` tx_hashes: [2](#0-1) 

However, the subsequent loop iterates over `txs_in_blocks`, which is keyed by block hash — one entry per distinct block containing a found transaction: [3](#0-2) 

For each distinct block, the server unconditionally performs:

1. **Full block load** — `get_block(&block_hash)` deserializes the entire block from the DB
2. **CBMT proof** — `CBMT::build_merkle_proof` iterates all transactions in the block
3. **Witnesses root** — `block.calc_witnesses_root()` hashes all witness data
4. **Uncle load** — `get_block_uncles(&block_hash)`
5. **Extension load** — `get_block_extension(&block_hash)` [4](#0-3) 

After the loop, `reply_proof` calls `mmr.gen_proof(items_positions)` with up to 1000 positions: [5](#0-4) 

There is no check on `txs_in_blocks.len()`. When all 1000 tx_hashes are in different blocks, `txs_in_blocks.len() == 1000` and all five per-block operations execute 1000 times. The limit check is therefore insufficient: it bounds the number of tx_hashes but not the number of distinct block loads, which is the actual cost driver.

---

### Impact Explanation

A single well-crafted `GetTransactionsProof` message triggers:
- 1000 full block deserializations from RocksDB
- 1000 CBMT proof constructions (each iterating all txs in a block)
- 1000 witnesses-root hash computations
- 1000 uncle block loads
- 1000 block extension loads
- 1 MMR proof for 1000 leaf positions

This is the maximum-cost request the handler can process. Because no ban is applied for a structurally valid request, an attacker can send this message in a tight loop, sustaining server congestion. The work is O(N × per_block_cost) rather than the intended O(N), where N = 1000.

---

### Likelihood Explanation

The attacker only needs to know 1000 transaction hashes confirmed in 1000 different blocks — all public information on any CKB chain with sufficient history. No privileged access, no PoW, no key material is required. The attack is executable by any peer that can open a light-client protocol connection.

---

### Recommendation

Add a limit on the number of distinct blocks that will be processed per request, separate from the tx_hash count limit. For example, after building `txs_in_blocks`, reject or truncate if `txs_in_blocks.len()` exceeds a configurable per-block limit (e.g., 50–100). Alternatively, enforce that all requested tx_hashes must reside within a bounded number of blocks, or apply per-peer rate limiting at the protocol handler level.

---

### Proof of Concept

1. Set up a CKB node with a chain of 1000+ blocks, each containing at least one unique transaction.
2. Collect one `tx_hash` from each of 1000 different blocks.
3. Send `GetTransactionsProof { tx_hashes: [tx_1, tx_2, ..., tx_1000], last_hash: tip_hash }` to the light-client protocol server.
4. Observe: the server executes the loop at lines 82–126 exactly 1000 times, performing 5000+ DB reads and 1000 CBMT/hash computations.
5. Repeat in a loop; no ban is issued because the request is structurally valid.
6. Compare CPU and DB read counts against a baseline request with all 1000 tx_hashes in the same block (loop executes once).

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L66-75)
```rust
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

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/lib.rs (L207-216)
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
```

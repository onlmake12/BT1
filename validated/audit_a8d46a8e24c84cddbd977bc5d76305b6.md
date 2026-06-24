All cited code references have been verified against the actual repository. Here is the validated audit report:

---

Audit Report

## Title
Missing Ancestor-Bound Check in `GetTransactionsProofProcess::execute` Enables Unbounded Resource Exhaustion — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary
`GetTransactionsProofProcess::execute` partitions requested transaction hashes into `found`/`missing` using only an `is_main_chain` check, with no verification that the transaction's block is at or before `last_block`. An unprivileged peer can supply `tx_hashes` referencing confirmed main-chain transactions in blocks *after* `last_hash`, causing the server to perform up to 1000 full block DB reads and CBMT proof constructions before failing with `InternalError` (HTTP 500), which never triggers a peer ban.

## Finding Description

**Step 1 — Entry guards pass.**
The tx count is bounded at 1000 and `last_hash` is verified to be on the main chain: [1](#0-0) 

**Step 2 — Partition only checks `is_main_chain`, not block height.**
The `found` set is built by checking only `snapshot.is_main_chain(&tx_info.block_hash)`. There is no `tx_info.block_number <= last_block.number()` guard: [2](#0-1) 

Transactions in blocks 101–200 when `last_hash` is block 100 all pass this filter.

**Step 3 — Expensive work is done for every out-of-range block.**
For each block in `txs_in_blocks`, the server performs a full block DB read, CBMT Merkle proof construction over all transactions in the block, an uncle hash fetch, and an extension fetch: [3](#0-2) 

**Step 4 — Out-of-range positions are passed to `gen_proof`.**
`leaf_index_to_pos(block.number())` is pushed for each out-of-range block without any bound check: [4](#0-3) 

**Step 5 — `gen_proof` fails with `InternalError`.**
`reply_proof` builds an MMR over `last_block.number() - 1` (covering only blocks 0..100) via `chain_root_mmr`: [5](#0-4) 

Then calls `gen_proof` with positions for blocks 101–200, which are outside the MMR size. The MMR library returns an error, and the handler returns `StatusCode::InternalError`: [6](#0-5) 

**Step 6 — `InternalError` (500) never bans the peer.**
`should_ban()` only returns `Some` for codes in the 400–499 range. `InternalError = 500` falls outside: [7](#0-6) 

The `received` handler therefore only logs a warning and does nothing else: [8](#0-7) 

The existing test at line 100 of the test file confirms `nc.not_banned(peer_index)` passes after an `InternalError` path: [9](#0-8) 

## Impact Explanation
Each malicious request causes up to 1000 full-block DB reads plus CBMT proof construction (O(n log n) in block transaction count) before returning an error. Because no ban is issued and there is no per-peer rate limiting, a single peer can repeat this indefinitely, exhausting I/O and CPU on any node serving the light-client protocol. This matches the **High** impact class: *Vulnerabilities which could easily crash a CKB node* and *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation
The light-client protocol is a standard P2P sub-protocol. Any peer that has observed the chain can trivially craft this message: pick any confirmed `last_hash` (e.g., an early block), then supply up to 1000 `tx_hashes` from blocks mined after it. No privilege, key, or hashpower is required. The attack is repeatable in a tight loop with no cooldown enforced by the server.

## Recommendation
In the partition at lines 54–64, additionally require `tx_info.block_number <= last_block.number()`:

```rust
.partition(|tx_hash| {
    snapshot
        .get_transaction_info(tx_hash)
        .map(|tx_info| {
            snapshot.is_main_chain(&tx_info.block_hash)
                && tx_info.block_number <= last_block.number()  // ADD THIS
        })
        .unwrap_or_default()
});
```

This moves the rejection to before any expensive DB work. Transactions in blocks after `last_hash` are treated as missing, which is semantically correct. Alternatively, return `StatusCode::InvalidRequest` (a 4xx code) when any found transaction exceeds `last_block.number()`, which would also trigger a peer ban.

## Proof of Concept
1. Mine 200 blocks; ensure blocks 101–200 each contain at least one non-coinbase transaction.
2. Send `GetTransactionsProof { last_hash: block[100].hash, tx_hashes: [tx from block 101, …, tx from block 200] }` (up to 1000 hashes).
3. Observe: server performs up to 100 full block fetches + 100 CBMT proofs, then returns `InternalError`.
4. Observe: peer is **not** banned — consistent with the existing test at line 100 of `util/light-client-protocol-server/src/tests/components/get_transactions_proof.rs`.
5. Repeat in a tight loop; measure monotonically increasing DB read and CPU time with no rate limiting applied.

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-49)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }

        let snapshot = self.protocol.shared.snapshot();

        let last_block_hash = self.message.last_hash().to_entity();
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendTransactionsProof>(self.peer, self.nc)
                .await;
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

**File:** util/snapshot/src/lib.rs (L181-184)
```rust
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
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

**File:** util/light-client-protocol-server/src/lib.rs (L199-215)
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

**File:** util/light-client-protocol-server/src/tests/components/get_transactions_proof.rs (L97-101)
```rust
    let peer_index = PeerIndex::new(1);
    protocol.received(nc.context(), peer_index, data).await;

    assert!(nc.not_banned(peer_index));

```

The code evidence is concrete. Let me lay out exactly what the code shows.

---

**What `complete_headers` actually does (lines 137-148):** [1](#0-0) 

It calls `self.snapshot.get_block(&ancestor_header.hash())` and then only uses two fields from the result: `calc_uncles_hash()` and `extension()`.

**What `get_block()` actually reads (store.rs lines 42-70):** [2](#0-1) 

`get_block()` unconditionally calls `get_block_body(h)` (line 55), reading `COLUMN_BLOCK_BODY` (all transactions), plus `get_block_uncles`, `get_block_proposal_txs_ids`, and `get_block_extension`. The transaction body data is fetched but never used by `complete_headers`.

**What `get_blocks_proof.rs` does instead (lines 81-95):** [3](#0-2) 

It calls `get_block_header`, `get_block_uncles`, and `get_block_extension` individually — zero reads from `COLUMN_BLOCK_BODY`.

**The limit check:** [4](#0-3) 

There is a `GET_LAST_STATE_PROOF_LIMIT` guard, but it bounds the *count* of samples, not the *bytes* read per sample. On a chain with large blocks, each `get_block()` call can read megabytes of transaction data that is immediately discarded.

**Also: the `last_block` fetch at line 217:** [5](#0-4) 

This also calls `get_block()` for the last block hash, but only uses `last_block.number()` — another unnecessary `COLUMN_BLOCK_BODY` read.

---

### Title
Unnecessary `COLUMN_BLOCK_BODY` reads in `BlockSampler::complete_headers` via `get_block()` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary
`complete_headers` fetches full `BlockView` objects (including all transaction bodies) but only consumes `uncles_hash` and `extension`. An unprivileged light-client peer sending a valid `GetLastStateProof` message triggers `COLUMN_BLOCK_BODY` reads proportional to total transaction count across all sampled blocks. The `get_blocks_proof.rs` path correctly avoids this by calling `get_block_header` + `get_block_uncles` + `get_block_extension` directly.

### Finding Description
In `complete_headers` (lines 137–148), `self.snapshot.get_block(&ancestor_header.hash())` is called for every sampled block number. `get_block()` in `store/src/store.rs` (line 55) unconditionally reads `COLUMN_BLOCK_BODY` — the full serialized transaction list — even though `complete_headers` only needs `calc_uncles_hash()` and `extension()`. The transaction data is loaded into memory and immediately dropped. The same pattern applies to the `last_block` fetch at line 217, which only uses `last_block.number()`.

### Impact Explanation
For a chain with high-throughput blocks (e.g., 1000 transactions × ~500 bytes each = ~500 KB per block), a single `GetLastStateProof` request sampling N blocks causes N × avg_block_body_size bytes of unnecessary RocksDB reads. This amplifies I/O and memory pressure on the full node proportionally to block body size, not just block count. The `get_blocks_proof.rs` path reads 0 bytes from `COLUMN_BLOCK_BODY` for the same logical operation.

### Likelihood Explanation
Any peer that can send light-client protocol messages can trigger this. No PoW, no privileged role, no key material required. The `GET_LAST_STATE_PROOF_LIMIT` check bounds sample count but not per-sample I/O cost.

### Recommendation
Replace `get_block()` in `complete_headers` with the same pattern used in `get_blocks_proof.rs`:
- `get_block_header(hash)` for the header
- `get_block_uncles(hash)` to compute `calc_uncles_hash()`
- `get_block_extension(hash)` for the extension

Apply the same fix to the `last_block` fetch at line 217 (only `number()` is needed — use `get_block_header` instead).

### Proof of Concept
Differential benchmark: populate a chain with 1000-tx blocks. Send a valid `GetLastStateProof` message. Instrument `get_block_body` call count. Assert it is called N times (once per sampled block) in the `complete_headers` path, versus 0 times in the `get_blocks_proof` path for equivalent block count. Measure bytes read from `COLUMN_BLOCK_BODY`: `complete_headers` reads O(N × avg_block_body_size); the optimized path reads 0.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L137-148)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L216-218)
```rust
        let last_block = snapshot
            .get_block(&last_block_hash)
            .expect("block should be in store");
```

**File:** store/src/store.rs (L42-70)
```rust
    fn get_block(&self, h: &packed::Byte32) -> Option<BlockView> {
        let header = self.get_block_header(h)?;
        if let Some(freezer) = self.freezer()
            && header.number() > 0
            && header.number() < freezer.number()
        {
            let raw_block = freezer.retrieve(header.number()).expect("block frozen")?;
            let raw_block_reader =
                packed::BlockReader::from_compatible_slice(&raw_block).expect("checked data");
            if raw_block_reader.calc_header_hash().as_slice() == h.as_slice() {
                return Some(raw_block_reader.to_entity().into_view());
            }
        }
        let body = self.get_block_body(h);
        let uncles = self
            .get_block_uncles(h)
            .expect("block uncles must be stored");
        let proposals = self
            .get_block_proposal_txs_ids(h)
            .expect("block proposal_ids must be stored");
        let extension_opt = self.get_block_extension(h);

        let block = if let Some(extension) = extension_opt {
            BlockView::new_unchecked_with_extension(header, uncles, body, proposals, extension)
        } else {
            BlockView::new_unchecked(header, uncles, body, proposals)
        };
        Some(block)
    }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L81-95)
```rust
        for block_hash in found {
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
            block_headers.push(header.data());

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
```

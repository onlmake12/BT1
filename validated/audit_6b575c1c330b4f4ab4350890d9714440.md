### Title
Block Filter Data Not Committed to Block Header Enables Targeted Transaction Censorship for Light Clients — (`block-filter/src/filter.rs`)

---

### Summary

CKB's block filter protocol (`SupportProtocols::Filter`) serves Golomb-Coded Set (GCS) filter data to light clients so they can identify blocks containing their transactions. The filter data is computed off-chain by the full node and is **not committed to the block header** — there is no `filter_root` in the CKB block header. The filter hash chain is not anchored to any consensus-verified structure. Additionally, `build_filter_data` silently produces incomplete filters when input cells cannot be resolved. A malicious full node can serve a consistent but incorrect filter chain that omits specific lock hashes or type script hashes, causing targeted light clients to permanently miss transactions relevant to them — a direct censorship analog to the Linea `l2MessagingBlocksOffsets` issue.

---

### Finding Description

**Root cause 1 — Silent incomplete filter construction:**

`build_filter_data` in `util/types/src/utilities/block_filter.rs` iterates over each non-cellbase transaction input and looks up the input cell to add its lock hash and type script hash to the GCS filter. When the input cell cannot be resolved, the out point is pushed to `missing_out_points` and the lock hash is **silently omitted** from the filter: [1](#0-0) 

In `build_filter_data_for_block`, the caller only logs a `warn!` for missing out points and then **commits the incomplete filter data to the database unconditionally**. The comment "This should only happen during testing" is not enforced: [2](#0-1) 

**Root cause 2 — Filter hash chain not anchored to block header:**

`calc_filter_hash` computes `blake2b(parent_block_filter_hash || filter_data_hash)`. This chain is self-referential but is **not anchored to the block header's `transactions_root`** or any other consensus-verified field. The block header contains no `filter_root` commitment: [3](#0-2) 

**Root cause 3 — Filter data served without proof of correctness:**

`GetBlockFiltersProcess::execute` serves raw filter bytes alongside block hashes. There is no Merkle proof or any other mechanism allowing the receiver to verify the filter was correctly derived from the block's transactions: [4](#0-3) 

The `BlockFilters` message schema confirms: it carries only `start_number`, `block_hashes`, and `filters` — no proof field: [5](#0-4) 

Similarly, `BlockFilterCheckPoints` and `BlockFilterHashes` carry only hashes, with no anchor to the block header: [6](#0-5) 

---

### Impact Explanation

A malicious full node advertising `BLOCK_FILTER` capability can:

1. Compute a filter chain that omits the lock hash of a targeted user's address.
2. Serve this internally consistent (but incomplete) filter chain — each filter hash correctly chains from the previous, so the light client's hash-chain verification passes.
3. The light client never downloads the block containing the targeted user's transaction, because the filter does not match the user's lock hash.
4. The user's transaction is effectively invisible to the light client, with no error or indication of censorship.

Because the filter data is not committed to the block header, a light client **cannot verify filter completeness without downloading the full block** — which defeats the purpose of the filter protocol. This is a targeted, undetectable censorship vector against light clients.

---

### Likelihood Explanation

Any peer running the `Filter` protocol (`SupportProtocols::Filter`) can execute this attack against any light client that connects to it. No privileged role is required. Light clients connecting to a single malicious node are fully vulnerable. The attack is undetectable without cross-referencing filter data from multiple independent peers. The `missing_out_points` path also shows this can occur non-maliciously (e.g., during a reorg or DB inconsistency), producing incorrect filters that are silently committed. [7](#0-6) 

---

### Recommendation

1. **Commit filter data to the block header**: Add a `filter_root` field to the block header so that filter data correctness is consensus-enforced. This is the strongest fix.
2. **Provide a derivation proof**: When serving `BlockFilters`, include a Merkle proof that the filter was correctly derived from the block's `transactions_root`.
3. **Treat `missing_out_points` as a hard error**: Do not commit incomplete filter data. If input cells cannot be resolved, abort filter construction and log an error rather than silently producing an incomplete filter.
4. **Light client cross-referencing**: Light clients should request filter data from multiple peers and reject inconsistent responses.

---

### Proof of Concept

A malicious node modifies `build_filter_data_for_block` to skip adding a specific target lock hash to the GCS filter, then commits the modified filter with a correctly chained filter hash. When a light client requests filters via `GetBlockFilters`, it receives the modified filter. The light client computes `blake2b(parent_filter_hash || filter_data_hash)` and finds it matches the `BlockFilterHashes` response — the chain is internally consistent. The light client's GCS query for the target lock hash returns false, so it does not download the block. The targeted transaction is permanently hidden from the light client with no detectable error. [8](#0-7) [9](#0-8)

### Citations

**File:** util/types/src/utilities/block_filter.rs (L15-47)
```rust
pub fn build_filter_data<P: FilterDataProvider>(
    provider: P,
    transactions: &[TransactionView],
) -> (Vec<u8>, Vec<packed::OutPoint>) {
    let mut filter_writer = Cursor::new(Vec::new());
    let mut filter = build_gcs_filter(&mut filter_writer);
    let mut missing_out_points = Vec::new();
    for tx in transactions {
        if !tx.is_cellbase() {
            for out_point in tx.input_pts_iter() {
                if let Some(input_cell) = provider.cell(&out_point) {
                    filter.add_element(input_cell.calc_lock_hash().as_slice());
                    if let Some(type_script) = input_cell.type_().to_opt() {
                        filter.add_element(type_script.calc_script_hash().as_slice());
                    }
                } else {
                    missing_out_points.push(out_point);
                }
            }
        }
        for output_cell in tx.outputs() {
            filter.add_element(output_cell.calc_lock_hash().as_slice());
            if let Some(type_script) = output_cell.type_().to_opt() {
                filter.add_element(type_script.calc_script_hash().as_slice());
            }
        }
    }
    filter
        .finish()
        .expect("flush to memory writer should be OK");
    let filter_data = filter_writer.into_inner();
    (filter_data, missing_out_points)
}
```

**File:** util/types/src/utilities/block_filter.rs (L50-61)
```rust
pub fn calc_filter_hash(
    parent_block_filter_hash: &packed::Byte32,
    filter_data: &packed::Bytes,
) -> [u8; 32] {
    blake2b_256(
        [
            parent_block_filter_hash.as_slice(),
            filter_data.calc_raw_data_hash().as_slice(),
        ]
        .concat(),
    )
}
```

**File:** block-filter/src/filter.rs (L124-172)
```rust
    fn build_filter_data_for_block(&self, header: &HeaderView) {
        debug!(
            "Start building filter data for block: {}, hash: {:#x}",
            header.number(),
            header.hash()
        );
        let db = self.shared.store();
        if db.get_block_filter_hash(&header.hash()).is_some() {
            debug!(
                "Filter data for block {:#x} already exists. Skip building.",
                header.hash()
            );
            return;
        }
        let parent_block_filter_hash = if header.is_genesis() {
            Byte32::zero()
        } else {
            db.get_block_filter_hash(&header.parent_hash())
                .expect("parent block filter data stored")
        };

        let transactions = db.get_block_body(&header.hash());
        let transactions_size: usize = transactions.iter().map(|tx| tx.data().total_size()).sum();
        let provider = WrappedChainDB::new(db);
        let (filter_data, missing_out_points) = build_filter_data(provider, &transactions);
        for out_point in missing_out_points {
            warn!(
                "Unable to find the input cell for the out_point: {:#x}, \
                Skip adding it to the filter. This should only happen during testing.",
                out_point
            );
        }
        let db_transaction = db.begin_transaction();
        db_transaction
            .insert_block_filter(
                &header.hash(),
                &(filter_data.clone().into()),
                &parent_block_filter_hash,
            )
            .expect("insert_block_filter should be ok");
        db_transaction.commit().expect("commit should be ok");
        debug!(
            "Inserted filter data for block: {}, hash: {:#x}, filter data size: {}, transactions size: {}",
            header.number(),
            header.hash(),
            filter_data.len(),
            transactions_size
        );
    }
```

**File:** sync/src/filter/get_block_filters_process.rs (L33-85)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        if latest >= start_number {
            let mut block_hashes = Vec::new();
            let mut filters = Vec::new();
            let mut current_content_size = 0;
            current_content_size += 8; // Size of start_number
            current_content_size += 4 * 2; // Size of the header field `full-size` of `block_hash` and `block_filter`
            let mut block_number = start_number;
            for _ in 0..BATCH_SIZE {
                if let Some(block_hash) = active_chain.get_block_hash(block_number) {
                    if let Some(block_filter) = active_chain.get_block_filter(&block_hash) {
                        if current_content_size
                            + block_hash.as_slice().len()
                            + 4
                            + block_filter.as_slice().len()
                            + 4
                            >= (1.8 * 1024.0 * 1024.0) as usize
                        {
                            // Break if the encoded size of `block_hash` + `block_filter` + `start_number` + molecule header increase reaches 1.8MB, to avoid frame size too large
                            break;
                        }
                        current_content_size +=
                            block_hash.as_slice().len() + block_filter.as_slice().len() + 4;
                        block_hashes.push(block_hash);
                        filters.push(block_filter);
                    } else {
                        break;
                    }
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(1) else {
                    break;
                };
                block_number = next_block_number;
            }
            let content = packed::BlockFilters::new_builder()
                .start_number(start_number)
                .block_hashes(block_hashes)
                .filters(filters)
                .build();
            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
        } else {
            Status::ignored()
        }
    }
```

**File:** util/gen-types/schemas/extensions.mol (L215-219)
```text
table BlockFilters {
    start_number:   Uint64,
    block_hashes:   Byte32Vec,
    filters:        BytesVec,
}
```

**File:** util/gen-types/schemas/extensions.mol (L225-238)
```text
table BlockFilterHashes {
    start_number:               Uint64,
    parent_block_filter_hash:   Byte32,
    block_filter_hashes:        Byte32Vec,
}

struct GetBlockFilterCheckPoints {
    start_number:   Uint64,
}

table BlockFilterCheckPoints {
    start_number:           Uint64,
    block_filter_hashes:    Byte32Vec,
}
```

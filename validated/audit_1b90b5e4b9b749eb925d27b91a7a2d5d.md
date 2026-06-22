### Title
Fixed SipHash Key `(0, 0)` in GCS Block Filter Enables Targeted False Positive Injection - (File: `util/types/src/utilities/block_filter.rs`)

### Summary

CKB's block filter subsystem constructs Golomb-Coded Set (GCS) filters using SipHash-2-4 with both keys hardcoded to zero (`SipHasher24Builder::new(0, 0)`). Because the hash function is fully deterministic and publicly known, any unprivileged transaction sender can precompute which script hashes produce false positives in any block's filter and craft transactions to force light clients to download blocks that contain no relevant transactions. This is the direct analog of the external report's "missing IV/key specification" vulnerability class: a cryptographic primitive is invoked without proper key material, making its outputs predictable and manipulable by an attacker.

### Finding Description

**Root cause — `build_gcs_filter` in `util/types/src/utilities/block_filter.rs`:**

```rust
fn build_gcs_filter(out: &mut dyn Write) -> GCSFilterWriter<'_, SipHasher24Builder> {
    GCSFilterWriter::new(out, SipHasher24Builder::new(0, 0), M, P)
}
```

SipHash-2-4 is a keyed hash function that takes two 64-bit keys `(k0, k1)`. Setting both to zero makes the function completely deterministic and predictable to any external party. The GCS filter maps each element (lock-script hash or type-script hash) through `SipHash(0, 0, element)` to a position in the filter. Because the key is fixed and public, an attacker can invert this mapping offline for any target value.

**Contrast with Bitcoin BIP 158**, which derives the key from the block hash:
```
k0 = block_hash[0..8]
k1 = block_hash[8..16]
```
This makes the key block-specific and unpredictable (it depends on the PoW nonce), so an attacker cannot precompute false positives before a block is mined. CKB's implementation omits this step entirely.

**Code path from attacker input to filter construction:**

1. `block-filter/src/filter.rs` → `build_filter_data_for_block` calls `build_filter_data` [1](#0-0) 
2. `build_filter_data` in `util/types/src/utilities/block_filter.rs` calls `build_gcs_filter` for every block's transactions [2](#0-1) 
3. `build_gcs_filter` constructs the filter with `SipHasher24Builder::new(0, 0)` [3](#0-2) 
4. The filter is stored and served to light clients via the `BlockFilter` protocol handler [4](#0-3) 

**Exploit flow:**

1. Attacker identifies a target light client's watched script hash `H` (e.g., from a public address).
2. Attacker computes `SipHash(0, 0, H)` offline to determine `H`'s GCS bucket position.
3. Attacker searches for a script hash `H'` such that `SipHash(0, 0, H') mod (N·M)` falls in the same GCS range as `H` (trivially feasible with a fixed key; no PoW required).
4. Attacker crafts a transaction with a lock script or type script whose `calc_script_hash()` equals `H'` and submits it to the tx pool.
5. Once included in a block, the block's GCS filter contains a false positive for `H`.
6. The light client watching `H` downloads and processes the block, finding no relevant transaction.
7. Repeating this across many blocks sustains a targeted bandwidth-exhaustion DoS against the light client.

The attacker entry point is the standard `send_transaction` RPC, which is explicitly in scope as a "tx-pool submitter." No privileged access is required.

### Impact Explanation

Light clients rely on block filters to avoid downloading every block. A targeted false positive attack forces a light client to download and validate blocks that contain no relevant transactions. At scale (attacker submits one crafted transaction per block), the light client's effective bandwidth consumption approaches that of a full node, defeating the purpose of the light client protocol. This constitutes "severe degradation under realistic attacker input" for the light client subsystem. The attack does not affect full-node consensus or asset integrity, but it is a concrete, reachable denial-of-service against a supported protocol component.

### Likelihood Explanation

The attack requires only that the attacker pay normal transaction fees to get crafted transactions included in blocks. The precomputation is trivial: with `(k0, k1) = (0, 0)`, the SipHash outputs are fully deterministic and can be computed on any standard machine in microseconds per candidate. No special network position, leaked key, or privileged access is needed. The attacker can sustain the attack indefinitely at the cost of one transaction fee per block per targeted light client.

### Recommendation

Derive the SipHash key from the block hash, as Bitcoin's BIP 158 does:

```rust
fn build_gcs_filter(out: &mut dyn Write, block_hash: &[u8]) -> GCSFilterWriter<'_, SipHasher24Builder> {
    let k0 = u64::from_le_bytes(block_hash[0..8].try_into().unwrap());
    let k1 = u64::from_le_bytes(block_hash[8..16].try_into().unwrap());
    GCSFilterWriter::new(out, SipHasher24Builder::new(k0, k1), M, P)
}
```

This makes the key block-specific and unpredictable (it depends on the PoW nonce), preventing offline precomputation of false positives. The `build_filter_data` function and its callers in `block-filter/src/filter.rs` would need to pass the block hash through.

### Proof of Concept

```python
import siphash  # pip install siphash

# Fixed key used by CKB
k0, k1 = 0, 0

# Target: light client watches this lock script hash
target_hash = bytes.fromhex("abcd1234" * 8)  # 32-byte script hash

# GCS parameters from CKB source (M=784931, P=19)
M = 784931
N = 1  # number of elements in filter (simplified)

target_bucket = siphash.SipHash_2_4(k0.to_bytes(8,'little') + k1.to_bytes(8,'little'), target_hash).hash() % (N * M)

# Find H' that maps to same bucket (brute force, trivial with fixed key)
for i in range(10_000_000):
    candidate = i.to_bytes(32, 'little')
    bucket = siphash.SipHash_2_4(k0.to_bytes(8,'little') + k1.to_bytes(8,'little'), candidate).hash() % (N * M)
    if bucket == target_bucket:
        print(f"Found collision: {candidate.hex()} -> bucket {bucket}")
        # Use candidate as the script hash in a crafted lock script
        break
```

The attacker then constructs a CKB transaction whose lock script hashes to `candidate`, submits it via `send_transaction` RPC, and waits for inclusion. The resulting block's GCS filter will contain a false positive for `target_hash`, causing the targeted light client to download the block unnecessarily.

### Citations

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

**File:** util/types/src/utilities/block_filter.rs (L63-65)
```rust
fn build_gcs_filter(out: &mut dyn Write) -> GCSFilterWriter<'_, SipHasher24Builder> {
    GCSFilterWriter::new(out, SipHasher24Builder::new(0, 0), M, P)
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

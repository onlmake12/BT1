### Title
GCS Block Filter Uses Hardcoded Zero SipHash Keys, Enabling Targeted False-Positive Injection Against Light Clients - (`util/types/src/utilities/block_filter.rs`)

---

### Summary

The `build_gcs_filter` function in `util/types/src/utilities/block_filter.rs` initializes the GCS (Golomb-Coded Set) block filter's internal SipHash-2-4 function with hardcoded zero keys (`k0=0, k1=0`). Because the hash function is fully deterministic and publicly known, an unprivileged transaction sender can craft transactions whose lock/type script hashes deterministically produce false positives in the block filter for any targeted light client, forcing it to download and verify every block on the chain.

---

### Finding Description

The production `build_gcs_filter` helper, called by `build_filter_data` for every block processed by the `BlockFilter` service, is:

```rust
// util/types/src/utilities/block_filter.rs, line 63-64
fn build_gcs_filter(out: &mut dyn Write) -> GCSFilterWriter<'_, SipHasher24Builder> {
    GCSFilterWriter::new(out, SipHasher24Builder::new(0, 0), M, P)
}
```

`SipHasher24Builder::new(0, 0)` sets both 64-bit SipHash keys to zero. The GCS filter maps each element (a lock hash or type script hash) to a position in the filter via `SipHash(k0, k1, element) mod (N * M)`. With `k0=k1=0`, this mapping is a globally known, deterministic function.

`build_filter_data` feeds every lock hash and type script hash from every transaction's inputs and outputs into this filter:

```rust
// util/types/src/utilities/block_filter.rs, lines 26-38
filter.add_element(input_cell.calc_lock_hash().as_slice());
// ...
filter.add_element(output_cell.calc_lock_hash().as_slice());
```

This is the same root cause class as the external report: instead of using a properly randomized/keyed hash function (analogous to `keccak256`), a degenerate fixed-key variant is used (analogous to `bytes32(abi.encodePacked(...))`). In both cases the output is directly predictable from the input, stripping the security property the hash was meant to provide.

The `BlockFilter` service in `block-filter/src/filter.rs` calls `build_filter_data` for every block on the main chain and persists the result:

```rust
// block-filter/src/filter.rs, line 148
let (filter_data, missing_out_points) = build_filter_data(provider, &transactions);
```

This is live production code, not a test path.

---

### Impact Explanation

Light clients use the GCS block filter to decide which blocks to download and fully verify. A false positive causes a light client to download and verify a block that contains no transactions relevant to it, wasting bandwidth and CPU.

Because the SipHash keys are zero and globally known, an attacker who knows a light client's monitored script hash `H` (e.g., a wallet's lock script hash, which is derived from a public address) can:

1. Compute `bucket = SipHash(0, 0, H) mod (N * M)` for the target hash.
2. Find a script hash `H'` such that `SipHash(0, 0, H') mod (N * M) == bucket` — trivially achievable by brute force since the function is public and fast.
3. Deploy a transaction with a lock script that hashes to `H'`.
4. Every block containing that transaction will produce a false positive for queries on `H`.

By repeating this across every block, the attacker forces the light client to download every block, degrading it to a full-node download burden without full-node security guarantees. This is a targeted, sustained DoS against light clients.

---

### Likelihood Explanation

- **Entry path**: Submitting a transaction to the network is an unprivileged operation available to any participant.
- **Knowledge requirement**: The target light client's monitored script hashes are often publicly derivable from on-chain addresses.
- **Computation cost**: Finding a colliding `H'` requires only iterating over candidate script hashes and evaluating the public, zero-keyed SipHash — a trivial offline computation.
- **No special access needed**: No privileged role, no majority hashpower, no social engineering.

---

### Recommendation

Derive the SipHash keys from the block hash, as specified in BIP 158 for Bitcoin's compact block filters. Pass the block hash into `build_gcs_filter` and `build_filter_data`:

```rust
fn build_gcs_filter(out: &mut dyn Write, block_hash: &[u8]) -> GCSFilterWriter<'_, SipHasher24Builder> {
    let k0 = u64::from_le_bytes(block_hash[0..8].try_into().unwrap());
    let k1 = u64::from_le_bytes(block_hash[8..16].try_into().unwrap());
    GCSFilterWriter::new(out, SipHasher24Builder::new(k0, k1), M, P)
}
```

This makes the SipHash keys unpredictable per block, preventing an attacker from pre-computing collisions across blocks.

---

### Proof of Concept

Given a light client monitoring lock script hash `H`:

1. Compute `target = SipHash(0, 0, H) mod (N * M)` (N = number of elements in the filter, M = filter parameter imported from `golomb_coded_set`).
2. Iterate candidate 32-byte values `H'` until `SipHash(0, 0, H') mod (N * M) == target`.
3. Construct a lock script whose `calc_script_hash()` equals `H'`.
4. Submit a transaction with an output using that lock script.
5. The block filter for any block containing this transaction will match queries for `H`, causing the light client to download and verify that block unnecessarily.
6. Repeat for every block to force the light client to download the entire chain.

---

**Root cause file:** [1](#0-0) 

**Production caller:** [2](#0-1) 

**Elements added to the filter (attacker-controlled inputs):** [3](#0-2)

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

**File:** util/types/src/utilities/block_filter.rs (L63-65)
```rust
fn build_gcs_filter(out: &mut dyn Write) -> GCSFilterWriter<'_, SipHasher24Builder> {
    GCSFilterWriter::new(out, SipHasher24Builder::new(0, 0), M, P)
}
```

**File:** block-filter/src/filter.rs (L145-165)
```rust
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
```

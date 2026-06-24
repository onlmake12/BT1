Looking at the actual write path in `store/src/transaction.rs` and `block-filter/src/filter.rs` to assess whether the desynchronization precondition is realistically reachable.

Audit Report

## Title
`get_block_filter` RPC `.expect()` Panics on Missing Filter Hash in Inconsistent DB State — (`File: rpc/src/module/chain.rs`)

## Summary
The `get_block_filter` implementation in `ChainRpcImpl` calls `.expect("stored filter hash")` when filter data is present but the corresponding filter hash record is absent. While the normal write path is atomic (both records are committed in a single RocksDB transaction), the `AddBlockFilterHash` DB migration writes hashes in batches of 10,000 per transaction; an interrupted migration leaves blocks with filter data but no filter hash. Any subsequent `get_block_filter` RPC call for such a block triggers the panic. The impact is a local RPC API crash.

## Finding Description
The panic is at `rpc/src/module/chain.rs` lines 1738–1746:

```rust
Ok(store.get_block_filter(&block_hash).map(|data| {
    let hash = store
        .get_block_filter_hash(&block_hash)
        .expect("stored filter hash");   // panics if hash absent
    BlockFilter { data: data.into(), hash: hash.into() }
}))
```

The submitter's primary exploit path — "node crash during filter write" — is **factually incorrect**. In `block-filter/src/filter.rs` lines 156–164, both `COLUMN_BLOCK_FILTER` and `COLUMN_BLOCK_FILTER_HASH` are written inside a single `StoreTransaction` committed atomically via `db_transaction.commit()`. A crash mid-write causes RocksDB to roll back the entire transaction, leaving neither record written. The desynchronized state cannot arise from a normal node crash during filter building.

The realistic path to the inconsistent state is the `AddBlockFilterHash` migration (`util/migrate/src/migrations/add_block_filter_hash.rs` lines 56–82). This migration writes filter hashes in batches of 10,000 per RocksDB transaction. If the migration process is killed mid-batch, the in-progress batch is rolled back, leaving those blocks with filter data (`COLUMN_BLOCK_FILTER`, written by a prior step) but no filter hash (`COLUMN_BLOCK_FILTER_HASH`). If the node subsequently starts in this partially-migrated state and an RPC caller queries one of those block hashes, the `.expect()` fires.

The `COLUMN_BLOCK_FILTER` and `COLUMN_BLOCK_FILTER_HASH` are distinct column families (columns `"17"` and `"18"` in `db-schema/src/lib.rs`), so they can diverge across separate transactions.

## Impact Explanation
**Note (0–500 points): Local RPC API crash.** The panic terminates the RPC handler for that request. The precondition — filter data present without a corresponding filter hash — is not achievable by an unprivileged RPC caller through normal RPC calls alone; it requires a prior interrupted DB migration or manual DB manipulation. The impact is therefore scoped to a local RPC crash, not a remotely-induced node shutdown. The submitter's claim of "High" severity (remote node crash triggerable by any unprivileged caller) is not supported: the exploit path requires a pre-existing DB inconsistency that the caller did not cause.

## Likelihood Explanation
Low. The triggering condition requires the `AddBlockFilterHash` migration to have been interrupted mid-batch and the node to have started in that partially-migrated state. In practice, CKB's migration framework records the migration version only after completion, so an interrupted migration would be re-run on next startup before RPC is served. Manual DB manipulation requires local filesystem access. The condition is not reachable by a remote unprivileged caller under normal operating conditions.

## Recommendation
Replace `.expect()` with a graceful fallback:

```rust
Ok(store.get_block_filter(&block_hash).and_then(|data| {
    let hash = store.get_block_filter_hash(&block_hash)?;
    Some(BlockFilter { data: data.into(), hash: hash.into() })
}))
```

This returns `Ok(None)` when filter data is present but the hash is absent, matching the `Result<Option<BlockFilter>>` contract.

## Proof of Concept
1. Run a CKB node with block filters enabled until several blocks have filter data.
2. Interrupt the `AddBlockFilterHash` migration mid-run (e.g., `kill -9` during migration).
3. Start the node in the partially-migrated state (bypassing migration re-run, e.g., by manually setting the migration version key).
4. Call `get_block_filter` via RPC with the hash of a block whose filter hash was in the rolled-back batch.
5. **Observed**: `.expect("stored filter hash")` panics.
6. **Expected**: `Ok(None)` or a structured error response.
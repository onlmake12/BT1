### Title
Redundant `block_median_time` Recomputation Per Input in `SinceVerifier` — (`verification/src/transaction_verifier.rs`)

### Summary

`SinceVerifier::verify()` iterates over every transaction input and, for each input carrying a timestamp-based `since` value, calls `block_median_time()` with the **same** `parent_hash` argument on every iteration. `block_median_time` performs `median_time_block_count` (37 on mainnet) sequential RocksDB header lookups plus a sort on each call. Because the tip's `parent_hash` is constant across all inputs of a single transaction, the result is identical for every iteration, yet the full 37-lookup chain is re-executed once per input with no caching. An unprivileged actor can craft a transaction with the maximum number of inputs all carrying absolute timestamp-based `since` values and submit it to the tx-pool or embed it in a block, forcing every verifying node to perform O(N × 37) redundant DB reads instead of O(37).

### Finding Description

`SinceVerifier::verify()` loops over all resolved inputs: [1](#0-0) 

For each input whose `since` encodes an absolute timestamp, `verify_absolute_lock` is called, which unconditionally invokes: [2](#0-1) 

`block_median_time` on the verifier delegates to the trait default implementation: [3](#0-2) 

That trait default walks `median_block_count` ancestor headers one by one via `get_header_fields`, collecting timestamps, then sorts them: [4](#0-3) 

The argument passed every time is `self.tx_env.parent_hash()` — the tip's parent hash — which is **identical** for every input of the same transaction. There is no memoisation between iterations.

The same redundancy exists in `verify_relative_lock` for the `current_median_time` call: [5](#0-4) 

`SinceVerifier` is instantiated inside `TimeRelativeTransactionVerifier`, which is called both during tx-pool admission (`ContextualTransactionVerifier::verify`) and during contextual block verification (`BlockTxsVerifier::verify`): [6](#0-5) [7](#0-6) 

### Impact Explanation

`median_time_block_count` is 37 on mainnet (confirmed by the RPC documentation: "consecutive 37 blocks"). Each `block_median_time` call performs 37 sequential `get_header_fields` RocksDB reads plus a sort. A transaction is size-limited by `TRANSACTION_SIZE_LIMIT` (checked in the tx-pool) and by `max_block_bytes` (checked during block verification). A CKB `CellInput` is 44 bytes on the wire (36-byte `OutPoint` + 8-byte `since`). Even at a conservative 100 KB transaction limit, an attacker can pack ≈ 2,272 inputs, causing ≈ 84,064 redundant DB reads per transaction verification instead of 37. At the full block-size limit (~597 KB), the multiplier reaches ≈ 13,500 inputs → ≈ 499,500 redundant reads.

During **block verification**, every full node in the network re-executes this work for every such transaction in the block, amplifying the impact network-wide. During **tx-pool admission**, the node's verification thread is monopolised for the duration.

### Likelihood Explanation

The attack requires no special privilege. Any tx-pool submitter (`send_transaction` RPC) or block relayer can craft a transaction spending cells they control, setting every input's `since` field to an absolute timestamp value (flag bits `0x4000_0000_0000_0000`). The cells can be pre-created cheaply. The transaction passes all structural checks (`NonContextualTransactionVerifier`) and the `since` flags are valid, so the only gate is the size limit. The attack is repeatable across multiple transactions in the same block.

### Recommendation

Compute `current_median_time` (and, where applicable, the pre-CKB2021 `parent_median_time` for the tip) **once** before the input loop and reuse the cached value:

```rust
pub fn verify(&self) -> Result<(), Error> {
    // Compute once; identical for every input of this transaction.
    let cached_tip_median_time: Option<u64> = None; // lazy-init on first timestamp since
    // ... or eagerly:
    // let tip_median_time = self.block_median_time(&self.tx_env.parent_hash());

    for (index, (cell_meta, input)) in self
        .rtx
        .resolved_inputs
        .iter()
        .zip(self.rtx.transaction.inputs())
        .enumerate()
    {
        // pass tip_median_time into verify_absolute_lock / verify_relative_lock
        // instead of recomputing it inside each call
    }
    Ok(())
}
```

The `parent_median_time` used for relative-timestamp `since` (pre-CKB2021 path, line 717) depends on `info.block_hash` which differs per input and cannot be shared, but the `current_median_time` at line 719 is always the same and should be hoisted out.

### Proof of Concept

1. Create a wallet with a large number of live cells (e.g., 10,000 cells, each holding the minimum capacity).
2. Construct a single transaction consuming all 10,000 cells as inputs, each with `since = 0x4000_0000_0000_0001` (absolute timestamp, 1 ms).
3. Submit via `send_transaction` RPC.
4. Observe that `SinceVerifier::verify` triggers 10,000 × 37 = 370,000 `get_header_fields` RocksDB reads (vs. the 37 that would suffice) before the transaction is accepted or rejected.
5. Repeat with multiple such transactions in a crafted block submitted via `submit_block` to force every peer to re-execute the same redundant work during contextual block verification.

The redundant computation path is:

```
send_transaction RPC
  → TxPoolService::_process_tx
    → verify_rtx
      → ContextualTransactionVerifier::verify
        → TimeRelativeTransactionVerifier::verify
          → SinceVerifier::verify          ← outer loop over N inputs
            → verify_absolute_lock         ← per input
              → block_median_time(parent)  ← 37 DB reads, same result every time
``` [8](#0-7) [3](#0-2) [4](#0-3)

### Citations

**File:** verification/src/transaction_verifier.rs (L162-169)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
```

**File:** verification/src/transaction_verifier.rs (L626-630)
```rust
    fn block_median_time(&self, block_hash: &Byte32) -> u64 {
        let median_block_count = self.consensus.median_time_block_count();
        self.data_loader
            .block_median_time(block_hash, median_block_count)
    }
```

**File:** verification/src/transaction_verifier.rs (L651-656)
```rust
                Some(SinceMetric::Timestamp(timestamp)) => {
                    let parent_hash = self.tx_env.parent_hash();
                    let tip_timestamp = self.block_median_time(&parent_hash);
                    if tip_timestamp < timestamp {
                        return Err((TransactionError::Immature { index }).into());
                    }
```

**File:** verification/src/transaction_verifier.rs (L719-719)
```rust
                    let current_median_time = self.block_median_time(&parent_hash);
```

**File:** verification/src/transaction_verifier.rs (L735-758)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        for (index, (cell_meta, input)) in self
            .rtx
            .resolved_inputs
            .iter()
            .zip(self.rtx.transaction.inputs())
            .enumerate()
        {
            // ignore empty since
            let since: u64 = input.since().into();
            if since == 0 {
                continue;
            }
            let since = Since(since);
            // check remain flags
            if !since.flags_is_valid() {
                return Err((TransactionError::InvalidSince { index }).into());
            }

            // verify time lock
            self.verify_absolute_lock(index, since)?;
            self.verify_relative_lock(index, since, cell_meta)?;
        }
        Ok(())
```

**File:** traits/src/header_provider.rs (L32-50)
```rust
    fn block_median_time(&self, block_hash: &Byte32, median_block_count: usize) -> u64 {
        let mut timestamps: Vec<u64> = Vec::with_capacity(median_block_count);
        let mut block_hash = block_hash.clone();
        for _ in 0..median_block_count {
            let header_fields = self
                .get_header_fields(&block_hash)
                .expect("parent header exist");
            timestamps.push(header_fields.timestamp);
            block_hash = header_fields.parent_hash;

            if header_fields.number == 0 {
                break;
            }
        }

        // return greater one if count is even.
        timestamps.sort_unstable();
        timestamps[timestamps.len() >> 1]
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L403-456)
```rust
        let ret = resolved
            .par_iter()
            .enumerate()
            .map(|(index, tx)| {
                let wtx_hash = tx.transaction.witness_hash();

                if let Some(completed) = fetched_cache.get(&wtx_hash) {
                    TimeRelativeTransactionVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                            Arc::clone(&tx_env),
                        )
                        .verify()
                        .map_err(|error| {
                            BlockTransactionsError {
                                index: index as u32,
                                error,
                            }
                            .into()
                        })
                        .map(|_| (wtx_hash, *completed))
                } else {
                    ContextualTransactionVerifier::new(
                        Arc::clone(tx),
                        Arc::clone(&self.context.consensus),
                        self.context.store.as_data_loader(),
                        Arc::clone(&tx_env),
                    )
                    .verify(
                        self.context.consensus.max_block_cycles(),
                        skip_script_verify,
                    )
                    .map_err(|error| {
                        BlockTransactionsError {
                            index: index as u32,
                            error,
                        }
                        .into()
                    })
                    .map(|completed| (wtx_hash, completed))
                }.and_then(|result| {
                    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
                        DaoScriptSizeVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                        ).verify()?;
                    }
                    Ok(result)
                })
            })
            .skip(1) // skip cellbase tx
            .collect::<Result<Vec<(Byte32, Completed)>, Error>>()?;
```

**File:** tx-pool/src/util.rs (L85-131)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
```

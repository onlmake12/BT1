Audit Report

## Title
Redundant Per-Input `block_median_time` Calls in `SinceVerifier::verify()` Enable Cheap Resource-Exhaustion DoS - (File: `verification/src/transaction_verifier.rs`)

## Summary

`SinceVerifier::verify()` iterates over every resolved input and, for each input carrying a timestamp-based `since` field, calls `self.block_median_time(&parent_hash)` — a function that performs up to 37 sequential RocksDB header lookups. Because `parent_hash` is derived from `self.tx_env.parent_hash()` and is constant across all inputs in the same transaction, the same 37-lookup chain is re-executed once per input rather than once per transaction. An unprivileged sender can craft a large transaction with many timestamp-`since` inputs to force O(N × 37) store reads during tx-pool admission and block verification, with no proportional cost increase to the attacker.

## Finding Description

**Outer loop — `SinceVerifier::verify()`:**

The loop iterates over every resolved input and calls both `verify_absolute_lock` and `verify_relative_lock` per input with no pre-computation of invariant values. [1](#0-0) 

**Absolute timestamp branch — repeated call with constant argument:**

Inside `verify_absolute_lock`, the timestamp branch calls `self.block_median_time(&parent_hash)` where `parent_hash = self.tx_env.parent_hash()`. This value is identical for every input in the transaction. [2](#0-1) 

**Relative timestamp branch — second repeated call with the same constant argument:**

`verify_relative_lock` independently re-derives `parent_hash` from `self.tx_env.parent_hash()` and calls `self.block_median_time(&parent_hash)` again, doubling the redundancy for pre-CKB2021 relative timestamp inputs. [3](#0-2) 

**`block_median_time` is an O(37) sequential store walk:**

Each call walks up to 37 ancestor headers via `get_header_fields`, which is a RocksDB read per iteration. There is no caching of the result. [4](#0-3) 

`MEDIAN_TIME_BLOCK_COUNT` is hardcoded to 37: [5](#0-4) 

**Scale:**

`MAX_BLOCK_BYTES = TWO_IN_TWO_OUT_BYTES × TWO_IN_TWO_OUT_COUNT = 597 × 1,000 = 597,000 bytes`. [6](#0-5) 

A `CellInput` is 44 bytes (36-byte `OutPoint` + 8-byte `since`), yielding up to ~13,500 inputs per maximum-size transaction. With all inputs using absolute timestamp `since`, `block_median_time` is called ~13,500 times × 37 reads = ~499,500 sequential RocksDB reads for a single invariant value.

**Trigger paths:**

The `SinceVerifier` is invoked in the tx-pool admission path via `verify_rtx`: [7](#0-6) 

## Impact Explanation

This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The attacker's cost is proportional to transaction byte size only; the node's verification cost is O(N × 37) sequential store reads. The tx-pool worker thread processes submissions synchronously, so a sustained stream of maximum-size timestamp-`since` transactions can saturate the worker thread and degrade or halt normal transaction processing. The block verification path (`BlockTxsVerifier`) also invokes `SinceVerifier` per transaction, meaning accepted blocks containing such transactions impose the same amplified I/O cost on all verifying nodes.

## Likelihood Explanation

The attack requires only the ability to call the public `send_transaction` RPC or relay via P2P — no privileged access, no key material, no majority hashpower. Constructing a transaction with many inputs referencing live cells and setting timestamp-based `since` values is straightforward. The transaction will ultimately be rejected as `Immature`, but the full O(N × 37) verification cost is paid before rejection is returned. The attack is repeatable at low cost.

## Recommendation

Compute `block_median_time(self.tx_env.parent_hash())` exactly once before the input loop in `SinceVerifier::verify()` and pass the cached result into `verify_absolute_lock` and `verify_relative_lock`. Since `self.tx_env.parent_hash()` does not change between inputs, this reduces the cost from O(N × 37) to O(37) per transaction. The `parent_median_time(&info.block_hash)` call in the relative-timestamp branch varies per input (it depends on the cell's block hash) and cannot be similarly hoisted, but `current_median_time` (from `self.tx_env.parent_hash()`) can and should be computed once and passed in.

## Proof of Concept

1. On a devnet with pre-funded outputs, obtain ~13,500 live cells.
2. Construct a transaction spending all of them, setting each input's `since` field to an absolute timestamp value in the future (e.g., `0x4000_0000_FFFF_FFFF`).
3. Submit via `send_transaction` RPC.
4. The node runs `SinceVerifier::verify()`, which calls `block_median_time(&parent_hash)` ~13,500 times, each performing 37 RocksDB reads (~499,500 reads total for a single invariant value).
5. The transaction is rejected as `Immature`, but the node has already paid the full verification cost.
6. Repeat in a tight loop to sustain load on the tx-pool worker thread.
7. Instrument with RocksDB read counters or `strace` to confirm the read amplification.

### Citations

**File:** verification/src/transaction_verifier.rs (L651-657)
```rust
                Some(SinceMetric::Timestamp(timestamp)) => {
                    let parent_hash = self.tx_env.parent_hash();
                    let tip_timestamp = self.block_median_time(&parent_hash);
                    if tip_timestamp < timestamp {
                        return Err((TransactionError::Immature { index }).into());
                    }
                }
```

**File:** verification/src/transaction_verifier.rs (L704-719)
```rust
                    let proposal_window = self.consensus.tx_proposal_window();
                    let parent_hash = self.tx_env.parent_hash();
                    let epoch_number = self.tx_env.epoch_number(proposal_window);
                    let hardfork_switch = self.consensus.hardfork_switch();
                    let base_timestamp = if hardfork_switch
                        .ckb2021
                        .is_block_ts_as_relative_since_start_enabled(epoch_number)
                    {
                        self.data_loader
                            .get_header_fields(&info.block_hash)
                            .expect("header exist")
                            .timestamp
                    } else {
                        self.parent_median_time(&info.block_hash)
                    };
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

**File:** spec/src/consensus.rs (L55-55)
```rust
const MEDIAN_TIME_BLOCK_COUNT: usize = 37;
```

**File:** spec/src/consensus.rs (L83-84)
```rust
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** tx-pool/src/util.rs (L85-132)
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
}
```

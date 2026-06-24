Audit Report

## Title
Redundant Per-Input `block_median_time` Calls in `SinceVerifier::verify()` Enable Cheap Resource-Exhaustion DoS - (File: `verification/src/transaction_verifier.rs`)

## Summary

`SinceVerifier::verify()` iterates over every resolved input and, for each input carrying a timestamp-based `since` field, calls `self.block_median_time(&parent_hash)` — a function that performs up to 37 sequential store lookups. Because `parent_hash` is derived from `self.tx_env.parent_hash()` and is constant across all inputs in the same transaction, the same 37-lookup chain is re-executed once per input rather than once per transaction. An unprivileged sender can craft a large transaction with many timestamp-`since` inputs to force O(N × 37) store reads during tx-pool admission and block verification, with no proportional ongoing cost to the attacker.

## Finding Description

**Outer loop — `SinceVerifier::verify()`** (`verification/src/transaction_verifier.rs`, lines 735–758): The loop iterates over every resolved input and calls both `verify_absolute_lock` and `verify_relative_lock` per input with no pre-computation of invariant values.

**Absolute timestamp branch** (lines 651–657): Inside `verify_absolute_lock`, the timestamp branch calls `self.block_median_time(&parent_hash)` where `parent_hash = self.tx_env.parent_hash()`. This value is identical for every input in the transaction.

**Relative timestamp branch** (lines 704–719): `verify_relative_lock` independently re-derives `parent_hash` from `self.tx_env.parent_hash()` and calls `self.block_median_time(&parent_hash)` again, doubling the redundancy for pre-CKB2021 relative timestamp inputs.

**`block_median_time` is an O(37) sequential store walk** (`traits/src/header_provider.rs`, lines 32–50): Each call walks up to 37 ancestor headers via `get_header_fields` with no caching of the result. `MEDIAN_TIME_BLOCK_COUNT` is hardcoded to 37 (`spec/src/consensus.rs`, line 55).

**Scale**: `MAX_BLOCK_BYTES = 597 × 1,000 = 597,000 bytes` (`spec/src/consensus.rs`, lines 83–84). A `CellInput` is 44 bytes, yielding up to ~13,500 inputs per maximum-size transaction. With all inputs using absolute timestamp `since`, `block_median_time` is called ~13,500 times × 37 reads = ~499,500 sequential store reads for a single invariant value.

**Trigger path**: `SinceVerifier` is invoked in the tx-pool admission path via `verify_rtx` (`tx-pool/src/util.rs`, lines 85–132), which calls `ContextualTransactionVerifier` or `TimeRelativeTransactionVerifier`, both of which invoke `SinceVerifier::verify()`. The transaction is rejected as `Immature` only after the full O(N × 37) cost is paid.

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The attacker's upfront cost is acquiring live cells; the ongoing cost per attack submission is zero (rejected transactions pay no fees). The tx-pool worker thread processes submissions synchronously, so a sustained stream of maximum-size timestamp-`since` transactions can saturate the worker thread and degrade or halt normal transaction processing. The block verification path also invokes `SinceVerifier` per transaction, meaning accepted blocks containing such transactions impose the same amplified I/O cost on all verifying nodes.

## Likelihood Explanation

The attack requires only the ability to call the public `send_transaction` RPC or relay via P2P — no privileged access, no key material, no majority hashpower. Constructing a transaction with many inputs referencing live cells and setting timestamp-based `since` values is straightforward. The transaction will be rejected as `Immature`, but the full O(N × 37) verification cost is paid before rejection is returned. Since the cells remain unspent after rejection, the attacker can reuse the same cell set indefinitely, making the attack repeatable at near-zero marginal cost after the initial cell acquisition.

## Recommendation

Compute `block_median_time(self.tx_env.parent_hash())` exactly once before the input loop in `SinceVerifier::verify()` and pass the cached result into `verify_absolute_lock` and `verify_relative_lock`. Since `self.tx_env.parent_hash()` does not change between inputs, this reduces the cost from O(N × 37) to O(37) per transaction. The `parent_median_time(&info.block_hash)` call in the relative-timestamp branch varies per input (it depends on the cell's block hash) and cannot be similarly hoisted, but `current_median_time` (from `self.tx_env.parent_hash()`) can and should be computed once and passed in.

## Proof of Concept

1. On a devnet with pre-funded outputs, acquire ~13,500 live cells.
2. Construct a transaction spending all of them, setting each input's `since` field to an absolute timestamp value in the future (e.g., `0x4000_0000_FFFF_FFFF`).
3. Submit via `send_transaction` RPC.
4. The node runs `SinceVerifier::verify()`, which calls `block_median_time(&parent_hash)` ~13,500 times, each performing 37 store reads (~499,500 reads total for a single invariant value).
5. The transaction is rejected as `Immature`, but the node has already paid the full verification cost.
6. Repeat in a tight loop to sustain load on the tx-pool worker thread.
7. Instrument with RocksDB read counters or `strace` to confirm the read amplification.
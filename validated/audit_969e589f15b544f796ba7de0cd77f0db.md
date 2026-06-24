Audit Report

## Title
`max_tx_verify_cycles` Not Enforced as Hard Upper Bound in `_process_tx` — (`tx-pool/src/process.rs`, `tx-pool/src/util.rs`)

## Summary
In `_process_tx`, the cycle budget passed to `verify_rtx` is set directly to `declared_cycles` with no cap at `max_tx_verify_cycles`. A remote peer that relays a transaction whose script genuinely consumes exactly `declared_cycles > max_tx_verify_cycles` cycles will have that transaction accepted into the pool, because the only post-verification guard checks `declared == actual`, not `actual <= max_tx_verify_cycles`. This allows an attacker to bypass the operator-configured cycle limit, causing excessive CPU consumption during verification and pool pollution with transactions the operator intended to reject.

## Finding Description

**Root cause — `_process_tx` (`tx-pool/src/process.rs` L720):**

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

`max_cycles` is set to `declared_cycles` directly. There is no `min(declared_cycles, max_tx_verify_cycles)` cap. This value is passed verbatim to `verify_rtx` as the VM cycle budget.

**`verify_rtx` async path (`tx-pool/src/util.rs` L108):**

```rust
.verify_with_pause(max_tx_verify_cycles, command_rx)
```

The parameter named `max_tx_verify_cycles` inside `verify_rtx` is actually the `max_cycles = declared_cycles` value received from `_process_tx`. The VM runs with the declared value as its budget.

**Only post-verification guard (`tx-pool/src/process.rs` L736-749):**

```rust
if let Some(declared) = declared_cycles
    && declared != verified.cycles
{
    return Some((Err(Reject::DeclaredWrongCycles(declared, verified.cycles)), snapshot));
}
```

This rejects only when `declared ≠ actual`. When a script genuinely consumes exactly `declared_cycles` cycles and `declared_cycles > max_tx_verify_cycles`, `verified.cycles == declared`, so this guard does not fire. No other guard in `_process_tx` or `submit_entry` checks `declared_cycles > max_tx_verify_cycles`.

**Relay-layer check (`sync/src/relayer/transactions_process.rs` L63-74)** only gates on `max_block_cycles` (consensus-level), not `max_tx_verify_cycles` (operator-configured). A transaction with `max_tx_verify_cycles < declared_cycles ≤ max_block_cycles` passes this check.

**Exploit path:**
1. Attacker constructs a valid transaction whose script consumes exactly `N` cycles, where `max_tx_verify_cycles < N ≤ max_block_cycles`.
2. Attacker relays the transaction with `declared_cycles = N` via P2P.
3. Relay layer passes (only checks `≤ max_block_cycles`).
4. `_process_tx` sets `max_cycles = N`, VM runs to completion, `verified.cycles = N = declared`.
5. `DeclaredWrongCycles` guard does not fire.
6. Transaction enters the pending pool.

**Existing test gap:** `DeclaredWrongCyclesChunk` sets `max_tx_verify_cycles = 500` and relays with `declared = 538`, `actual = 537`. Rejection occurs because `538 ≠ 537` (`DeclaredWrongCycles`), not because `declared > max_tx_verify_cycles`. The case `declared == actual > max_tx_verify_cycles` is untested and unguarded.

## Impact Explanation

This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

- The VM runs for up to `max_block_cycles` instead of the operator-configured `max_tx_verify_cycles`, consuming excessive CPU per transaction.
- The pool fills with transactions the operator explicitly configured to reject, degrading block assembly and pool throughput.
- An attacker can repeat this with many transactions at low cost (only standard transaction fees required), causing sustained CPU exhaustion and pool congestion across nodes that have set a restrictive `max_tx_verify_cycles`.

## Likelihood Explanation

The attack requires only P2P relay access and the ability to construct a valid transaction with a loop-counting script that consumes exactly `N` cycles. No privileged access, hashpower, or key material beyond standard transaction fees is needed. The cycle count of a script is deterministic and can be measured precisely before submission. The attack is repeatable and cheap relative to the damage caused.

## Recommendation

In `_process_tx`, cap `max_cycles` at `max_tx_verify_cycles` when a declared value is present, and add an explicit early rejection:

```rust
// Early rejection
if let Some(declared) = declared_cycles {
    if declared > self.tx_pool_config.max_tx_verify_cycles {
        return Some((
            Err(Reject::ExceededMaximumCycles(declared, self.tx_pool_config.max_tx_verify_cycles)),
            snapshot,
        ));
    }
}

// Cap the VM budget
let max_cycles = declared_cycles
    .map(|d| d.min(self.tx_pool_config.max_tx_verify_cycles))
    .unwrap_or_else(|| self.consensus.max_block_cycles());
```

A new `Reject::ExceededMaximumCycles(Cycle, Cycle)` variant should be added to the `Reject` enum in `util/types/src/core/tx_pool.rs`.

## Proof of Concept

A unit test in `tx-pool/src/component/tests/chunk.rs` (which already sets `MAX_TX_VERIFY_CYCLES = 70_000_000` and builds a `TxPoolService`) should:

1. Construct a transaction whose script consumes exactly `MAX_TX_VERIFY_CYCLES + 1` cycles.
2. Call `_process_tx` with `declared_cycles = Some(MAX_TX_VERIFY_CYCLES + 1)` and a `command_rx` (async/chunk path).
3. Assert the result is `Err(Reject::ExceededMaximumCycles(...))` (currently it returns `Ok`, demonstrating the missing guard).

The existing infrastructure at `tx-pool/src/component/tests/chunk.rs` L28 (`MAX_TX_VERIFY_CYCLES = 70_000_000`) and L121-125 (which already shows the queue accepting `MAX_TX_VERIFY_CYCLES + 1` without rejection) provides the scaffolding for this test. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/process.rs (L719-732)
```rust
        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;
```

**File:** tx-pool/src/process.rs (L736-749)
```rust
        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }
```

**File:** tx-pool/src/util.rs (L101-115)
```rust
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
```

**File:** tx-pool/src/component/tests/chunk.rs (L28-29)
```rust
const MAX_TX_VERIFY_CYCLES: u64 = 70_000_000;
const UNUSED_SNAPSHOT_COLUMNS: u32 = 1;
```

**File:** tx-pool/src/component/tests/chunk.rs (L121-125)
```rust
    assert!(
        queue
            .add_tx(tx1.clone(), false, remote(MAX_TX_VERIFY_CYCLES + 1))
            .unwrap()
    );
```

**File:** test/src/specs/tx_pool/declared_wrong_cycles.rs (L52-65)
```rust
        let tx = node0.new_transaction_spend_tip_cellbase();

        relay_tx(&net, node0, tx, ALWAYS_SUCCESS_SCRIPT_CYCLE + 1);

        let result = wait_until(5, || {
            let tx_pool_info = node0.get_tip_tx_pool_info();
            tx_pool_info.orphan.value() == 0 && tx_pool_info.pending.value() == 0
        });
        assert!(result, "Declared wrong cycles should be rejected");
    }

    fn modify_app_config(&self, config: &mut ckb_app_config::CKBAppConfig) {
        config.network.connect_outbound_interval_secs = 0;
        config.tx_pool.max_tx_verify_cycles = 500; // ALWAYS_SUCCESS_SCRIPT_CYCLE: u64 = 537
```

**File:** util/types/src/core/tx_pool.rs (L17-67)
```rust
pub enum Reject {
    /// Transaction fee lower than config
    #[error(
        "The min fee rate is {0}, requiring a transaction fee of at least {1} shannons, but the fee provided is only {2}"
    )]
    LowFeeRate(FeeRate, u64, u64),

    /// Transaction exceeded maximum ancestors count limit
    #[error("Transaction exceeded maximum ancestors count limit; try later")]
    ExceededMaximumAncestorsCount,

    /// Transaction exceeded maximum size limit
    #[error("Transaction size {0} exceeded maximum limit {1}")]
    ExceededTransactionSizeLimit(u64, u64),

    /// Transaction are replaced because the pool is full
    #[error("Transaction is replaced because the pool is full, {0}")]
    Full(String),

    /// Transaction already exists in transaction_pool
    #[error("Transaction({0}) already exists in transaction_pool")]
    Duplicated(Byte32),

    /// Malformed transaction
    #[error("Malformed {0} transaction")]
    Malformed(String, String),

    /// Declared wrong cycles
    #[error("Declared wrong cycles {0}, actual {1}")]
    DeclaredWrongCycles(Cycle, Cycle),

    /// Resolve failed
    #[error("Resolve failed {0}")]
    Resolve(OutPointError),

    /// Verification failed
    #[error("Verification failed {0}")]
    Verification(Error),

    /// Expired
    #[error("Expiry transaction, timestamp {0}")]
    Expiry(u64),

    /// RBF rejected
    #[error("RBF rejected: {0}")]
    RBFRejected(String),

    /// Invalidated by cell consuming Tx
    #[error("Invalidated: {0}")]
    Invalidated(String),
}
```

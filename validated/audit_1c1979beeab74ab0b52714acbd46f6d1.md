All code references check out. Let me verify the entry.rs usage of `get_transaction_weight` to complete the analysis.

Audit Report

## Title
Fee Admission Check Uses Byte Size Only, Ignoring CKB-VM Cycle Cost — (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size, never the computational weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). Because no second fee gate exists after `verify_rtx` returns the actual cycle count, an attacker can submit a minimal-size transaction containing a cycle-exhausting script, pay a trivial size-based fee, and force every receiving node to run the CKB-VM for up to `max_block_cycles` cycles during pool admission — a severe underpayment relative to the true computational cost imposed.

## Finding Description
`check_tx_fee` (`tx-pool/src/util.rs` L28–54) explicitly uses only `tx_size` for the minimum fee gate, with the code itself acknowledging the limitation:

```rust
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

This is called inside `pre_check` (`process.rs` L289–290) before any script execution. After `pre_check` passes, `_process_tx` (`process.rs` L720) sets the VM cycle cap:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [2](#0-1) 

For a locally submitted transaction (`declared_cycles = None`), `max_cycles` becomes the full consensus `max_block_cycles`. `verify_rtx` then runs the VM up to that cap. After `verify_rtx` returns the actual `verified.cycles`, the code at L751 simply constructs the pool entry with no second fee check:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
``` [3](#0-2) 

The weight-based function `get_transaction_weight` (`util/types/src/core/tx_pool.rs` L298–303) computes `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)` but is only invoked post-admission for sorting (`AncestorsScoreSortKey`, `entry.rs` L221–231) and eviction (`EvictKey`, `entry.rs` L234–247), never for the admission gate. [4](#0-3) [5](#0-4) 

For the relay path, `transactions_process.rs` L63–74 only bans peers whose `declared_cycles > max_block_cycles`; declaring exactly `max_block_cycles` is permitted, and the fee check still uses size only. [6](#0-5) 

## Impact Explanation
An attacker controlling valid UTXOs can submit transactions that pass the size-based fee gate (~200 shannons for a ~200-byte tx) while containing a lock/type script that consumes cycles up to the limit. Each such transaction forces every receiving node to run the CKB-VM for up to `max_block_cycles` cycles during pool admission. Sustained submission saturates tx-pool verification workers, delays block template assembly, and degrades node responsiveness across the network. This matches: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The entry path is fully unprivileged: any `send_transaction` RPC caller or P2P relay peer qualifies. The attacker needs valid UTXOs and a deployed looping script cell (a one-time cost), but the fee cost per transaction is negligible (~200 shannons ≈ 0.000002 CKB). The attack is repeatable as long as the attacker controls UTXOs. The relay path is also exploitable by declaring `declared_cycles = max_block_cycles` with a script that actually consumes that many cycles, since only `declared_cycles > max_block_cycles` triggers a ban. [7](#0-6) 

## Recommendation
Replace the size-only fee check in `check_tx_fee` with a weight-based check. For the pre-check stage (before cycles are known), use the declared cycles from the relay message as an upper bound, or use `max_tx_verify_cycles` as a conservative proxy:

```rust
let weight = get_transaction_weight(tx_size, declared_cycles.unwrap_or(max_tx_verify_cycles));
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

Alternatively, add a second fee check after `verify_rtx` returns the actual cycle count, using `get_transaction_weight(tx_size, verified.cycles)` to compute the correct minimum fee before constructing the `TxEntry` at `process.rs` L751. [8](#0-7) 

## Proof of Concept
1. Deploy a CKB script cell whose bytecode loops consuming cycles until the VM halts at the cycle limit.
2. Create a transaction spending any UTXO with that script as the lock, sized to ~200 bytes.
3. Submit via `send_transaction` RPC. The fee check passes (200 shannons ≥ size-based minimum at 1,000 shannons/KB).
4. The node's `verify_rtx` runs the script for up to `max_block_cycles` cycles before rejecting (cycle limit exceeded) or accepting.
5. Repeat with up to `max_ancestors_count = 25` chained descendants per UTXO to multiply impact.
6. Observe that verification workers are saturated and block template assembly is delayed.
7. For the relay path: relay the same transaction with `declared_cycles = max_block_cycles`; the relay handler permits it (only `declared_cycles > max_block_cycles` triggers a ban), and the fee check still uses size only. [9](#0-8)

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

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

**File:** tx-pool/src/process.rs (L751-751)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/component/entry.rs (L221-247)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
    }
}

impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
    }
```

**File:** sync/src/relayer/transactions_process.rs (L63-74)
```rust
        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }
```

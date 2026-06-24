Looking at the actual code to validate each claim in the report.

Audit Report

## Title
RBF Replacement Unconditionally Resets Pool Entry Timestamp, Defeating Expiry Eviction - (`tx-pool/src/pool.rs`, `tx-pool/src/component/entry.rs`)

## Summary
When RBF is enabled, every replacement transaction is inserted via `TxEntry::new(...)`, which stamps the current wall-clock time as the entry's `timestamp`. Because `remove_expired` evicts entries solely based on `entry.timestamp + expiry < now_ms`, an attacker can reset the expiry clock indefinitely by submitting a replacement just before the window closes. The cost per 12-hour cycle is approximately 300 shannons (~0.000003 CKB), making the attack effectively free.

## Finding Description
`TxEntry::new` unconditionally calls `unix_time_as_millis()` for every new entry, including RBF replacements:

```rust
// tx-pool/src/component/entry.rs L48-50
pub fn new(rtx: Arc<ResolvedTransaction>, cycles: Cycle, fee: Capacity, size: usize) -> Self {
    Self::new_with_timestamp(rtx, cycles, fee, size, unix_time_as_millis())
}
```

The expiry check in `remove_expired` compares only the entry's own timestamp:

```rust
// tx-pool/src/pool.rs L277
.filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
```

where `expiry = config.expiry_hours as u64 * 60 * 60 * 1000` (pool.rs L57).

In `_process_tx`, after verification, the replacement entry is constructed with a fresh timestamp:

```rust
// tx-pool/src/process.rs L751
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

Then `process_rbf` removes the old entry and `_submit_entry` inserts the new one (process.rs L136-137). `check_rbf` validates fee rules but never reads or forwards the old entry's timestamp. There is no mechanism to carry the original timestamp forward.

A secondary effect: `EvictKey` ordering (sort_key.rs L92-103) evicts entries with the **oldest** timestamp first when fee rates are equal. Resetting the timestamp to "now" also makes the attacker's entry less likely to be evicted by `limit_size`, compounding the attack.

## Impact Explanation
This matches **High — bad designs which could cause CKB network congestion with few costs**. An attacker holding multiple UTXOs can submit a family of transactions and replace each one just before expiry, occupying pool slots indefinitely. Because the timestamp reset also improves the attacker's `EvictKey` rank, these entries resist both `remove_expired` and size-limit eviction relative to legitimate stale transactions. At scale, this crowds out legitimate transactions from the mempool, degrading transaction propagation across nodes that have RBF enabled.

## Likelihood Explanation
Any unprivileged caller of `send_transaction` (RPC or P2P relay) can trigger this on any node with `min_rbf_rate > min_fee_rate`. The default configuration (`min_rbf_rate = 1500 shannons/KB`, `expiry_hours = 12`) requires only ~300 shannons of additional fee per replacement cycle. No special knowledge beyond the publicly documented defaults is needed. The attacker controls the timing entirely and can automate replacements.

## Recommendation
Preserve the original entry's timestamp across RBF replacements. Specifically:

1. In `process_rbf` (or `check_rbf`), extract the minimum `timestamp` among all conflicting entries being evicted.
2. Pass that timestamp to `TxEntry::new_with_timestamp(...)` when constructing the replacement entry, so the expiry deadline is inherited from the oldest replaced entry rather than reset to the current time.

`new_with_timestamp` already exists for this purpose (entry.rs L52-75); it only needs to be called from the RBF replacement path.

## Proof of Concept
1. Configure node: `min_fee_rate = 1000`, `min_rbf_rate = 1500`, `expiry_hours = 12`.
2. Submit **T1** spending UTXO `[A]` with fee 1000 shannons/KB. Pool records `T1.timestamp = T`.
3. At `T + 11h55m`, submit **T2** spending `[A]` with fee ≥ 1300 shannons/KB (satisfying `check_rbf` Rules #3/#4). Pool records `T2.timestamp = T + 11h55m`.
4. `remove_expired` at `T + 12h`: T1 is already gone; T2's deadline is `T + 11h55m + 12h` — safely in the future.
5. Repeat step 3 every ~11h55m. UTXO `[A]` remains locked in the pool indefinitely. Total cost after N cycles ≈ `initial_fee + N × 300 shannons`; after 1000 cycles (~500 days) ≈ 0.003 CKB.
6. Scale to many UTXOs to occupy a significant fraction of pool slots across RBF-enabled nodes.
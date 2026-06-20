### Title
Hardcoded `MAX_REPLACEMENT_CANDIDATES` Limit Causes Permanent RBF DOS for Long Transaction Chains - (File: `tx-pool/src/pool.rs`)

---

### Summary

The RBF (Replace-By-Fee) replacement candidate limit is hardcoded to 100 in `tx-pool/src/pool.rs`, while the ancestor chain length (`max_ancestors_count`) is fully operator-configurable. On nodes migrated from legacy configs, `max_ancestors_count` is forced to at least 1,000. This mismatch means any RBF attempt on the root of a chain with more than 100 descendants is permanently rejected, even when the submitter offers an arbitrarily high fee.

---

### Finding Description

In `tx-pool/src/pool.rs`, the maximum number of transactions that can be displaced by a single RBF operation is hardcoded as a compile-time constant:

```rust
const MAX_REPLACEMENT_CANDIDATES: usize = 100;
``` [1](#0-0) 

This constant is consumed inside `check_rbf()`, which counts the conflict transaction plus all its descendants and rejects the incoming RBF transaction if the total exceeds 100:

```rust
replace_count += descendants.len() + 1;
if replace_count > MAX_REPLACEMENT_CANDIDATES {
    return Err(Reject::RBFRejected(format!(
        "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
        replace_count, MAX_REPLACEMENT_CANDIDATES,
    )));
}
``` [2](#0-1) 

By contrast, `max_ancestors_count` — which controls how long a transaction chain the pool will accept — is a fully configurable field in `TxPoolConfig`: [3](#0-2) 

Critically, the legacy config migration path **forces** `max_ancestors_count` to at least `DEFAULT_MAX_ANCESTORS_COUNT = 1_000`:

```rust
max_ancestors_count: cmp::max(DEFAULT_MAX_ANCESTORS_COUNT, max_ancestors_count),
``` [4](#0-3) [5](#0-4) 

On such nodes the pool legally accepts chains of up to 1,000 transactions, yet `MAX_REPLACEMENT_CANDIDATES = 100` makes it impossible to RBF-replace the root of any chain longer than 100 transactions, regardless of the fee offered.

`MAX_REPLACEMENT_CANDIDATES` is not present in `TxPoolConfig`, is not exposed in `ckb.toml`, and cannot be changed without recompiling the binary. [6](#0-5) 

---

### Impact Explanation

An unprivileged RPC caller (`send_transaction`) who has built a pending chain of 101 or more transactions — which the pool accepts when `max_ancestors_count ≥ 101` — is permanently unable to use RBF to replace the root transaction. The rejection is deterministic and unconditional: no fee, however large, can overcome it. This constitutes a complete DOS of the RBF fee-bumping mechanism for any chain that exceeds the hardcoded threshold.

**Impact: Medium** — RBF is rendered non-functional for long chains; stuck low-fee transactions cannot be accelerated.

---

### Likelihood Explanation

Nodes that have ever run a legacy CKB configuration have `max_ancestors_count` forced to at least 1,000 by the migration path. On those nodes, chains of 101–1,000 transactions are routinely accepted, and the mismatch is always present. A user who builds such a chain (e.g., a payment processor or batch sender) and later needs to fee-bump the root will hit this rejection unconditionally.

**Likelihood: Medium** — Requires `max_ancestors_count > 100` (the legacy default) and a chain longer than 100 transactions, both of which are realistic in production.

---

### Recommendation

Add `max_replacement_candidates` as a configurable field in `TxPoolConfig` alongside `max_ancestors_count`, with a sensible default (e.g., `max_ancestors_count` itself, or a separately tunable value). Replace the hardcoded constant in `check_rbf()` with `self.config.max_replacement_candidates`. This mirrors how `min_fee_rate`, `min_rbf_rate`, and `max_ancestors_count` are already handled.

---

### Proof of Concept

1. Start a CKB node with `max_ancestors_count = 1000` (legacy default or explicit config).
2. Submit transaction T1 (low fee) via `send_transaction`.
3. Submit T2 spending T1's output, T3 spending T2's output, … T101 spending T100's output. Each is accepted because each has ≤ 100 ancestors.
4. Submit T1' — a replacement for T1 with a very high fee (satisfying `min_rbf_rate`).
5. `check_rbf()` counts T1's descendants: 100 descendants + 1 = 101 > `MAX_REPLACEMENT_CANDIDATES`. The node returns:
   ```
   RBF rejected: Tx conflict with too many txs, conflict txs count: 101, expect <= 100
   ```
6. T1 is permanently stuck; no fee bump is possible via RBF. [7](#0-6)

### Citations

**File:** tx-pool/src/pool.rs (L33-33)
```rust
const MAX_REPLACEMENT_CANDIDATES: usize = 100;
```

**File:** tx-pool/src/pool.rs (L611-624)
```rust
        // Rule #5, the replaced tx's descendants can not more than 100
        // and the ancestor of the new tx don't have common set with the replaced tx's descendants
        let mut replace_count: usize = 0;
        let mut all_conflicted = conflicts.clone();
        let ancestors = self.pool_map.calc_ancestors(&short_id);
        for conflict in conflicts.iter() {
            let descendants = self.pool_map.calc_descendants(&conflict.id);
            replace_count += descendants.len() + 1;
            if replace_count > MAX_REPLACEMENT_CANDIDATES {
                return Err(Reject::RBFRejected(format!(
                    "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
                    replace_count, MAX_REPLACEMENT_CANDIDATES,
                )));
            }
```

**File:** util/app-config/src/configs/tx_pool.rs (L10-43)
```rust
#[derive(Clone, Debug, Serialize)]
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
    /// txs need to pay larger fee rate than this for RBF
    #[serde(with = "FeeRateDef")]
    pub min_rbf_rate: FeeRate,
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
    #[serde(default = "default_max_tx_verify_workers")]
    pub max_tx_verify_workers: usize,
    /// max ancestors size limit for a single tx
    pub max_ancestors_count: usize,
    /// rejected tx time to live by days
    pub keep_rejected_tx_hashes_days: u8,
    /// rejected tx count limit
    pub keep_rejected_tx_hashes_count: u64,
    /// The file to persist the tx pool on the disk when tx pool have been shutdown.
    ///
    /// By default, it is a subdirectory of 'tx-pool' subdirectory under the data directory.
    #[serde(default)]
    pub persisted_data: PathBuf,
    /// The recent reject record database directory path.
    ///
    /// By default, it is a subdirectory of 'tx-pool' subdirectory under the data directory.
    #[serde(default)]
    pub recent_reject: PathBuf,
    /// The expiration time for pool transactions in hours
    pub expiry_hours: u8,
}
```

**File:** util/app-config/src/legacy/tx_pool.rs (L16-16)
```rust
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```

**File:** util/app-config/src/legacy/tx_pool.rs (L129-129)
```rust
            max_ancestors_count: cmp::max(DEFAULT_MAX_ANCESTORS_COUNT, max_ancestors_count),
```

### Title
Low-cost Transaction DoS Permanently Blocks Cell Spending When RBF Is Disabled — (`tx-pool/src/pool.rs`)

### Summary

When RBF is disabled (`min_rbf_rate <= min_fee_rate`), an attacker can submit a minimum-fee transaction spending a specific live cell. Because no replacement mechanism exists in this configuration, the transaction occupies the cell's input slot in the pool for the full `expiry_hours` window (default 12 hours) with no way for a legitimate higher-fee transaction to displace it. The attacker can repeat this indefinitely at negligible cost (~242 shannons per 12-hour cycle), permanently preventing a targeted cell from being spent.

### Finding Description

`enable_rbf()` in `tx-pool/src/pool.rs` returns `false` whenever `min_rbf_rate <= min_fee_rate`:

```rust
pub fn enable_rbf(&self) -> bool {
    self.config.min_rbf_rate > self.config.min_fee_rate
}
```

When RBF is disabled, `min_replace_fee()` immediately returns `None`:

```rust
pub fn min_replace_fee(&self, tx: &TxEntry) -> Option<Capacity> {
    if !self.enable_rbf() {
        return None;
    }
    ...
}
```

This means `check_rbf` is never invoked for conflicting transactions. Any new transaction that shares an input with an existing pool entry is rejected outright — there is no path by which a higher-fee transaction can displace the incumbent, regardless of how much fee it offers.

The pool's only eviction mechanism for such a transaction is time-based expiry:

```rust
pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
    let now_ms = ckb_systemtime::unix_time_as_millis();
    let removed: Vec<_> = self
        .pool_map
        .iter()
        .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
        ...
}
```

where `expiry = expiry_hours * 3_600_000 ms` (default 12 hours).

**Attack steps:**

1. Identify a live cell (OutPoint) that a victim intends to spend.
2. Submit a minimum-fee transaction spending that cell. At `min_fee_rate = 1000 shannons/KB` and a ~242-byte transaction, the fee is ~242 shannons (≈ 0.00000000242 CKB, essentially zero).
3. The transaction occupies the cell's input slot for up to 12 hours. Every legitimate transaction spending the same cell is rejected.
4. One second before expiry, the attacker resubmits a fresh minimum-fee transaction spending the same cell, resetting the 12-hour window.
5. Repeat indefinitely.

The configuration that enables this attack (`min_rbf_rate = min_fee_rate`) is explicitly supported by the codebase and documented in the config comment: *"min_rbf_rate > min_fee_rate means RBF is enabled."* Node operators may set them equal to disable RBF, and the code path is fully reachable.

### Impact Explanation

A targeted live cell can be permanently prevented from being spent at a cost of ~242 shannons per 12-hour period. Concretely:

- **Griefing / front-running**: An attacker who observes a pending high-value transaction (e.g., a DeFi settlement, a time-sensitive unlock) can front-run it with a dust-fee conflicting transaction, blocking the legitimate transaction for 12 hours. The attacker can repeat this to block it indefinitely.
- **Cell locking**: Any cell whose owner relies on a node with RBF disabled can have its funds effectively frozen at negligible attacker cost.
- **Miner revenue loss**: Miners on the targeted node lose the higher-fee transaction and receive only the dust fee (or nothing if the attacker's tx is never mined).

### Likelihood Explanation

The attack requires the targeted node to have RBF disabled (`min_rbf_rate <= min_fee_rate`). The default production config ships with RBF enabled (`min_rbf_rate = 1500 > min_fee_rate = 1000`). However:

- The configuration is explicitly supported and documented.
- Operators may disable RBF intentionally (e.g., to match Bitcoin's default policy or for simplicity).
- The attacker only needs to find one such node; they do not control the node's configuration themselves.
- The attack cost is negligible, making it attractive even for low-probability targets.

### Recommendation

1. **Enforce a minimum RBF margin**: Require `min_rbf_rate > min_fee_rate` at startup and reject configurations where they are equal, making RBF non-optional.
2. **Always allow fee-bumping**: Even when RBF is "disabled," permit a new transaction to replace an existing one if it pays strictly more fee than the incumbent, so that a higher-fee transaction can always displace a lower-fee one.
3. **Reduce the default expiry**: Shorten `expiry_hours` so that the blocking window is smaller, increasing the attacker's cost per cycle.

### Proof of Concept

```
# Node configured with min_rbf_rate = min_fee_rate (RBF disabled)

# Attacker: submit a minimum-fee tx spending cell X (cost: ~242 shannons)
ckb-cli tx send --tx attacker_min_fee_tx_spending_cell_X.json

# Victim: attempt to submit a higher-fee tx spending the same cell X
ckb-cli tx send --tx victim_high_fee_tx_spending_cell_X.json
# => Error: PoolRejectedDuplicatedTransaction (or conflict rejection)
# Victim's tx is rejected; attacker's dust-fee tx holds the slot for 12 hours.

# Attacker resubmits just before expiry to reset the 12-hour window.
# Repeat indefinitely at ~242 shannons per cycle.
```

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** tx-pool/src/pool.rs (L80-99)
```rust
    /// Check whether tx-pool enable RBF
    pub fn enable_rbf(&self) -> bool {
        self.config.min_rbf_rate > self.config.min_fee_rate
    }

    /// The least required fee rate to allow tx to be replaced
    pub fn min_replace_fee(&self, tx: &TxEntry) -> Option<Capacity> {
        if !self.enable_rbf() {
            return None;
        }

        let mut conflicts = vec![self.get_pool_entry(&tx.proposal_short_id()).unwrap()];
        let descendants = self.pool_map.calc_descendants(&tx.proposal_short_id());
        let descendants = descendants
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        conflicts.extend(descendants);
        self.calculate_min_replace_fee(&conflicts, tx.size)
    }
```

**File:** tx-pool/src/pool.rs (L270-288)
```rust
    // Expire all transaction (and their dependencies) in the pool.
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-18)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
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

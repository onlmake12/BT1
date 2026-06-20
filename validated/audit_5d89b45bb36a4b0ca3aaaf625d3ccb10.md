The code evidence is conclusive. Here is the analysis:

---

**Root cause — the `From` conversion always enforces a floor of 1000:** [1](#0-0) 

```rust
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
``` [2](#0-1) 

```rust
max_ancestors_count: cmp::max(DEFAULT_MAX_ANCESTORS_COUNT, max_ancestors_count),
```

This means any operator-configured value **below 1000 is silently overridden to 1000**.

**Every config load goes through this path — it is not optional:** [3](#0-2) 

`CKBAppConfig::load_from_slice` always deserializes into `legacy::CKBAppConfig` first, then calls `.into()`. There is no direct deserialization path that bypasses the legacy conversion.

**The bundled template sets `max_ancestors_count = 25`:** [4](#0-3) 

An operator who runs `ckb init` and uses the default config gets `max_ancestors_count = 25` in their `ckb.toml`, but the node actually runs with 1000.

**The pool check uses `<=`, so a chain of exactly 999 ancestors + 1 new tx = 1000 is accepted:** [5](#0-4) 

```rust
let mut ancestors_count = ancestors.len() + 1;
...
if ancestors_count <= self.max_ancestors_count {
    self._record_ancestors(entry, ancestors, parents);
    return Ok(evicted);
}
```

---

### Title
Legacy `TxPoolConfig` conversion silently overrides operator-configured `max_ancestors_count` to 1000 — (`util/app-config/src/legacy/tx_pool.rs`)

### Summary
The `From<legacy::TxPoolConfig> for crate::TxPoolConfig` conversion unconditionally applies `cmp::max(1_000, max_ancestors_count)`, meaning any operator-configured value below 1000 (including the bundled default of 25) is silently replaced with 1000. Since every config load goes through this conversion, the operator's intended ancestor-chain limit is never enforced.

### Finding Description
In `util/app-config/src/legacy/tx_pool.rs` line 129, the conversion writes:

```rust
max_ancestors_count: cmp::max(DEFAULT_MAX_ANCESTORS_COUNT, max_ancestors_count),
```

where `DEFAULT_MAX_ANCESTORS_COUNT = 1_000`. This is applied to **all** configs, not just configs that omit the field. The bundled `resource/ckb.toml` ships with `max_ancestors_count = 25`, but after conversion the live value is 1000.

`CKBAppConfig::load_from_slice` (the sole config loading entry point) always routes through `legacy::CKBAppConfig` and calls `.into()`, so there is no way to load a config that bypasses this floor. [6](#0-5) 

### Impact Explanation
An attacker who can submit transactions (via RPC or P2P relay) can build ancestor chains of up to 999 depth instead of the operator-intended 25. This is a 40× amplification. Deep ancestor chains increase the cost of block template construction (ancestor-set sorting and fee aggregation must traverse the full chain) and increase relay bandwidth. The attacker pays fees for each transaction, but the economic cost is bounded and the damage to the node is unbounded within the 1000-deep limit.

### Likelihood Explanation
The bug is triggered by the default configuration. Any operator who runs `ckb init` and does not manually raise `max_ancestors_count` above 1000 is affected. The exploit requires only the ability to submit transactions, which is available to any P2P peer or any client with RPC access.

### Recommendation
Change line 129 of `util/app-config/src/legacy/tx_pool.rs` from:

```rust
max_ancestors_count: cmp::max(DEFAULT_MAX_ANCESTORS_COUNT, max_ancestors_count),
```

to:

```rust
max_ancestors_count,
```

The `cmp::max` floor was presumably intended to supply a default when the field is absent, but the `#[serde(default)]` mechanism (or the `Default` impl) is the correct place for that. The `From` conversion should pass the deserialized value through unchanged. [7](#0-6) 

### Proof of Concept
1. Run `ckb init --chain dev` — the generated `ckb.toml` contains `max_ancestors_count = 25`.
2. Start the node with `ckb run`.
3. Submit a chain of 999 dependent transactions (each spending the output of the previous one) via `send_transaction` RPC.
4. Submit a 1000th transaction spending the output of tx #999.
5. Observe: all 1000 transactions are accepted into the pool. `ancestors_count = 1000 <= max_ancestors_count = 1000` passes the check at `pool_map.rs:598`.
6. Expected: rejection at depth 26 (`ancestors_count = 26 > max_ancestors_count = 25`). [8](#0-7) [9](#0-8)

### Citations

**File:** util/app-config/src/legacy/tx_pool.rs (L16-16)
```rust
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```

**File:** util/app-config/src/legacy/tx_pool.rs (L79-99)
```rust
impl Default for TxPoolConfig {
    fn default() -> Self {
        Self {
            max_mem_size: None,
            max_tx_pool_size: DEFAULT_MAX_TX_POOL_SIZE,
            max_cycles: None,
            max_verify_cache_size: None,
            max_conflict_cache_size: None,
            max_committed_txs_hash_cache_size: None,
            max_tx_verify_workers: default_max_tx_verify_workers(),
            keep_rejected_tx_hashes_days: default_keep_rejected_tx_hashes_days(),
            keep_rejected_tx_hashes_count: default_keep_rejected_tx_hashes_count(),
            min_fee_rate: DEFAULT_MIN_FEE_RATE,
            min_rbf_rate: DEFAULT_MIN_RBF_RATE,
            max_tx_verify_cycles: DEFAULT_MAX_TX_VERIFY_CYCLES,
            max_ancestors_count: DEFAULT_MAX_ANCESTORS_COUNT,
            persisted_data: Default::default(),
            recent_reject: Default::default(),
            expiry_hours: DEFAULT_EXPIRY_HOURS,
        }
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L102-136)
```rust
impl From<TxPoolConfig> for crate::TxPoolConfig {
    fn from(input: TxPoolConfig) -> Self {
        let TxPoolConfig {
            max_mem_size: _,
            max_tx_pool_size,
            max_cycles: _,
            max_verify_cache_size: _,
            max_conflict_cache_size: _,
            max_committed_txs_hash_cache_size: _,
            max_tx_verify_workers,
            keep_rejected_tx_hashes_days,
            keep_rejected_tx_hashes_count,
            min_fee_rate,
            min_rbf_rate,
            max_tx_verify_cycles,
            max_ancestors_count,
            persisted_data,
            recent_reject,
            expiry_hours,
        } = input;

        Self {
            max_tx_pool_size,
            min_fee_rate,
            min_rbf_rate,
            max_tx_verify_cycles,
            max_tx_verify_workers,
            max_ancestors_count: cmp::max(DEFAULT_MAX_ANCESTORS_COUNT, max_ancestors_count),
            keep_rejected_tx_hashes_days,
            keep_rejected_tx_hashes_count,
            persisted_data,
            recent_reject,
            expiry_hours,
        }
    }
```

**File:** util/app-config/src/app_config.rs (L265-274)
```rust
    pub fn load_from_slice(slice: &[u8]) -> Result<Self, ExitCode> {
        let legacy_config: legacy::CKBAppConfig = toml::from_slice(slice)?;
        for field in legacy_config.deprecated_fields() {
            eprintln!(
                "WARN: the option \"{}\" in configuration files is deprecated since v{}.",
                field.path, field.since
            );
        }
        Ok(legacy_config.into())
    }
```

**File:** resource/ckb.toml (L216-216)
```text
max_ancestors_count = 25
```

**File:** tx-pool/src/component/pool_map.rs (L588-601)
```rust
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }
```

### Title
Legacy `TxPoolConfig` Conversion Silently Overrides Operator-Configured `max_ancestors_count` with Hardcoded Floor, Allowing Tx-Pool Ancestor-Chain Limit Bypass - (`util/app-config/src/legacy/tx_pool.rs`)

### Summary

In `util/app-config/src/legacy/tx_pool.rs`, the `From<TxPoolConfig> for crate::TxPoolConfig` conversion enforces a hardcoded floor of `DEFAULT_MAX_ANCESTORS_COUNT = 1_000` on the `max_ancestors_count` field, silently overriding any operator-configured value below 1,000. The production `ckb.toml` ships with `max_ancestors_count = 25`. Any node whose config is deserialized through the legacy path will have its configured limit silently replaced with 1,000, allowing an unprivileged tx-pool submitter to build ancestor chains 40× longer than the operator intended, degrading or denying tx-pool service.

### Finding Description

`util/app-config/src/legacy/tx_pool.rs` defines a legacy `TxPoolConfig` struct used for backward-compatible deserialization of old `ckb.toml` files. Its `From` conversion into the canonical `crate::TxPoolConfig` contains:

```rust
// util/app-config/src/legacy/tx_pool.rs  line 129
max_ancestors_count: cmp::max(DEFAULT_MAX_ANCESTORS_COUNT, max_ancestors_count),
```

where `DEFAULT_MAX_ANCESTORS_COUNT = 1_000` (line 16). This means any operator-supplied value smaller than 1,000 is silently discarded and replaced with 1,000.

The production default config (`resource/ckb.toml`, line 216) ships with:

```toml
max_ancestors_count = 25
```

When a node whose config is processed through the legacy path starts up, `TxPool::new` receives `config.max_ancestors_count = 1_000` and passes it directly to `PoolMap::new`:

```rust
// tx-pool/src/pool.rs  line 59
pool_map: PoolMap::new(config.max_ancestors_count),
```

`PoolMap` stores this as `self.max_ancestors_count` and uses it as the sole gate in `check_and_record_ancestors`:

```rust
// tx-pool/src/component/pool_map.rs  line 598
if ancestors_count <= self.max_ancestors_count {
    self._record_ancestors(entry, ancestors, parents);
    return Ok(evicted);
}
```

Because the effective limit is 1,000 instead of 25, any tx-pool submitter (RPC `send_transaction` or P2P relay) can build a chain of up to 1,000 unconfirmed parent transactions and have every link accepted. The intended protection against long-chain performance degradation is completely bypassed.

This is the direct CKB analog of the reported Ethereum bug: a hardcoded constant (`24 ether` / `1_000`) is used in a state-transition path instead of the context-specific configurable parameter (`maxStakingAmountPerValidator` / `max_ancestors_count`), allowing state to exceed the operator's intended bound.

### Impact Explanation

An unprivileged tx-pool submitter can submit a chain of up to 1,000 ancestor transactions to a node running with the legacy config path. Each additional ancestor forces `PoolMap` to traverse and update the full ancestor set on every insertion. At 1,000 ancestors the quadratic bookkeeping cost can saturate the tx-pool service thread, causing sustained high CPU usage, delayed block-template generation, and effective denial of service for legitimate transaction submission on that node.

### Likelihood Explanation

The legacy config path is the backward-compatibility deserialization route for any `ckb.toml` that was written before the current config schema was introduced. Nodes that have been running since earlier versions and have not regenerated their config file will use this path. The production `ckb.toml` template sets `max_ancestors_count = 25`, so every such node is affected. The attack requires only the ability to call `send_transaction` via RPC or relay a transaction over P2P—no privileged access is needed.

### Recommendation

Remove the `cmp::max` floor in the legacy conversion so the operator's explicit setting is respected:

```rust
// util/app-config/src/legacy/tx_pool.rs
max_ancestors_count: max_ancestors_count,
```

If a safety floor is genuinely required for backward compatibility, it should be documented and set to a value consistent with the current default (e.g., 25), not the old default of 1,000:

```rust
max_ancestors_count: cmp::max(25, max_ancestors_count),
```

### Proof of Concept

1. Start a CKB node whose `ckb.toml` is deserialized through the legacy path with `max_ancestors_count = 25`.
2. Observe via `get_tx_pool_info` that the node reports `max_ancestors_count` effectively as 1,000 (the floor applied by the conversion).
3. Submit a chain of 26 transactions where each spends the output of the previous one via `send_transaction`.
4. All 26 are accepted into the pool; the 26th would have been rejected under the intended limit of 25.
5. Extend the chain to 1,000 transactions; all are accepted, consuming quadratic bookkeeping work in `check_and_record_ancestors` and degrading tx-pool responsiveness. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** util/app-config/src/legacy/tx_pool.rs (L16-16)
```rust
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```

**File:** util/app-config/src/legacy/tx_pool.rs (L129-129)
```rust
            max_ancestors_count: cmp::max(DEFAULT_MAX_ANCESTORS_COUNT, max_ancestors_count),
```

**File:** tx-pool/src/pool.rs (L55-59)
```rust
    pub fn new(config: TxPoolConfig, snapshot: Arc<Snapshot>) -> TxPool {
        let recent_reject = Self::build_recent_reject(&config);
        let expiry = config.expiry_hours as u64 * 60 * 60 * 1000;
        TxPool {
            pool_map: PoolMap::new(config.max_ancestors_count),
```

**File:** tx-pool/src/component/pool_map.rs (L595-601)
```rust
        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }
```

**File:** resource/ckb.toml (L215-216)
```text
max_tx_verify_cycles = 70_000_000
max_ancestors_count = 25
```

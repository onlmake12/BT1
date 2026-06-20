### Title
Legacy Config Conversion Silently Overrides `max_ancestors_count` to 1000, Bypassing Operator-Configured DoS Limit — (`util/app-config/src/legacy/tx_pool.rs`)

---

### Summary

The legacy tx-pool config conversion in `util/app-config/src/legacy/tx_pool.rs` applies `cmp::max(DEFAULT_MAX_ANCESTORS_COUNT, max_ancestors_count)` when converting to the current `TxPoolConfig`. Because `DEFAULT_MAX_ANCESTORS_COUNT = 1_000`, any operator-configured value smaller than 1000 (including the production default of 25) is silently overridden to 1000. This is structurally identical to the Zap Protocol bug: a `max(computed, floor)` pattern where the floor is non-zero and applies even when the entity should receive a lower value, allowing the intended limit to be bypassed.

---

### Finding Description

In `util/app-config/src/legacy/tx_pool.rs`, the `From<TxPoolConfig> for crate::TxPoolConfig` conversion contains:

```rust
max_ancestors_count: cmp::max(DEFAULT_MAX_ANCESTORS_COUNT, max_ancestors_count),
```

where `DEFAULT_MAX_ANCESTORS_COUNT = 1_000`. [1](#0-0) [2](#0-1) 

The production config template and the integration test template both set `max_ancestors_count = 25`: [3](#0-2) [4](#0-3) 

When a node operator runs with the legacy config format and sets `max_ancestors_count = 25`, the conversion computes `max(1000, 25) = 1000`. The node silently runs with a 40× higher ancestor chain limit than the operator intended, with no warning or error.

The `max_ancestors_count` limit is enforced in `tx-pool/src/component/pool_map.rs` inside `check_and_record_ancestors`: [5](#0-4) [6](#0-5) 

The effective limit used at runtime is `self.max_ancestors_count`, which is sourced directly from the config. If the config was loaded via the legacy path, this value is 1000 instead of 25.

---

### Impact Explanation

The `max_ancestors_count` limit is an explicit DoS protection mechanism. The CHANGELOG documents its purpose:

> "Txs with long ancestors chain affect tx pool performance. we limit max ancestors count of a single tx to resolve this issue, tx pool will reject txs which ancestors count large than the limit. The default `max_ancestors_count` is 25." [7](#0-6) 

With the limit silently raised to 1000, an attacker can submit a chain of up to 1000 unconfirmed transactions. Each new transaction in the chain triggers ancestor traversal in `get_tx_ancenstors`, which is O(n) in the chain depth. Submitting many such chains causes quadratic work in the tx-pool, degrading node performance and potentially causing it to fall behind in processing legitimate transactions.

The `Reject::ExceededMaximumAncestorsCount` error that should protect the node is never triggered until the chain reaches 1000 depth instead of 25. [8](#0-7) 

---

### Likelihood Explanation

Any node that:
1. Was originally configured with the legacy config format (common for long-running nodes that predate the config format change), and
2. Has `max_ancestors_count` set to any value below 1000 (including the documented default of 25)

is affected. The legacy config path is still compiled and active in the codebase. An attacker needs only to submit a chain of transactions via the public `send_transaction` RPC endpoint — no authentication or special privilege is required. [9](#0-8) 

---

### Recommendation

Remove the `cmp::max` floor in the legacy config conversion. The operator's configured value should be used directly:

```diff
- max_ancestors_count: cmp::max(DEFAULT_MAX_ANCESTORS_COUNT, max_ancestors_count),
+ max_ancestors_count,
```

If a minimum safe value is desired, it should be documented and enforced with an explicit error or warning rather than a silent override. [10](#0-9) 

---

### Proof of Concept

1. Start a CKB node using the legacy config format with `max_ancestors_count = 25`.
2. Observe via logs or RPC that the effective `max_ancestors_count` is 1000 (not 25), because the legacy conversion applies `max(1000, 25)`.
3. Submit a chain of 1000 child-spends-parent transactions via `send_transaction`. Each transaction is accepted until the 1000th, whereas with the intended limit of 25, the 26th would be rejected with `PoolRejectedTransactionByMaxAncestorsCountLimit`.
4. The integration test `test_send_transaction_exceeded_maximum_ancestors_count` in `rpc/src/tests/module/pool.rs` already hardcodes `MAX_ANCESTORS_COUNT = 1000`, confirming the legacy default is what the runtime uses. [11](#0-10)

### Citations

**File:** util/app-config/src/legacy/tx_pool.rs (L16-16)
```rust
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
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

**File:** resource/ckb.toml (L216-216)
```text
max_ancestors_count = 25
```

**File:** test/template/ckb.toml (L90-90)
```text
max_ancestors_count = 25
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

**File:** tx-pool/src/component/pool_map.rs (L626-628)
```rust
        } else {
            return Err(Reject::ExceededMaximumAncestorsCount);
        }
```

**File:** CHANGELOG.md (L1752-1756)
```markdown
- #1788: Limit tx max ancestors count (@jjyr)

    Txs with long ancestors chain affect tx pool performance. we limit max ancestors count of a single tx to resolve this issue, tx pool will reject txs which ancestors count large than the limit.

    The default `max_ancestors_count` is 25.
```

**File:** util/types/src/core/tx_pool.rs (L25-26)
```rust
    #[error("Transaction exceeded maximum ancestors count limit; try later")]
    ExceededMaximumAncestorsCount,
```

**File:** rpc/src/error.rs (L100-103)
```rust
    /// (-1105): The in-pool ancestors count must be less than or equal to the config option `tx_pool.max_ancestors_count`
    ///
    /// Pool rejects a large package of chained transactions to avoid certain kinds of DoS attacks.
    PoolRejectedTransactionByMaxAncestorsCountLimit = -1105,
```

**File:** rpc/src/tests/module/pool.rs (L138-188)
```rust
fn test_send_transaction_exceeded_maximum_ancestors_count() {
    const MAX_ANCESTORS_COUNT: u64 = 1000;

    let suite = setup(always_success_consensus());

    let store = suite.shared.store();
    let tip = store.get_tip_header().unwrap();
    let tip_block = store.get_block(&tip.hash()).unwrap();
    let mut parent_tx_hash = tip_block.transactions().first().unwrap().hash();

    // generate 2000 child-spends-parent txs
    for i in 0..(MAX_ANCESTORS_COUNT + 1) {
        let input = CellInput::new(OutPoint::new(parent_tx_hash.clone(), 0), 0);
        let output = CellOutputBuilder::default()
            .capacity(
                Capacity::bytes(1000)
                    .unwrap()
                    .safe_sub(Capacity::shannons(i * 41 * 1000))
                    .unwrap(),
            )
            .lock(always_success_cell().2.clone())
            .build();
        let cell_dep = CellDep::new_builder()
            .out_point(OutPoint::new(always_success_transaction().hash(), 0))
            .build();
        let tx = TransactionBuilder::default()
            .input(input)
            .output(output)
            .output_data(packed::Bytes::default())
            .cell_dep(cell_dep)
            .build();
        let new_tx: ckb_jsonrpc_types::Transaction = tx.data().into();
        let response = suite.rpc(&RpcTestRequest {
            id: 42,
            jsonrpc: "2.0".to_string(),
            method: "send_transaction".to_string(),
            params: vec![json!(new_tx), json!("passthrough")],
        });
        if i != MAX_ANCESTORS_COUNT {
            assert_eq!(response.error.to_string(), "null".to_string());
        } else {
            assert!(
                response
                    .error
                    .to_string()
                    .contains("ExceededMaximumAncestorsCount")
            );
        }
        parent_tx_hash = tx.hash();
    }
}
```

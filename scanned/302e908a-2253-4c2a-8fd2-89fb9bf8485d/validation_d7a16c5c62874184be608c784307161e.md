### Title
`WeightUnitsFlow` Fee Estimator Uses Hardcoded `MAX_BLOCK_BYTES` Constant Instead of Consensus-Configured `max_block_bytes` — (File: `util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

The `WeightUnitsFlow` fee-rate estimator's core `do_estimate` function hardcodes the compile-time constant `MAX_BLOCK_BYTES` when computing how much weight each mined block is expected to drain from the mempool. The actual block-byte limit is a per-chain consensus parameter (`Consensus::max_block_bytes`) that can be set to any value via the chain spec. When the two values diverge, every fee-rate estimate produced by this algorithm is systematically wrong, causing RPC callers to either overpay or have their transactions permanently stuck.

---

### Finding Description

`util/fee-estimator/src/estimator/weight_units_flow.rs` imports and uses the compile-time constant `MAX_BLOCK_BYTES` from `ckb_chain_spec::consensus`:

```rust
use ckb_chain_spec::consensus::MAX_BLOCK_BYTES;   // line 57
```

Inside `do_estimate`, the amount of weight that each block is expected to remove from the mempool is computed as:

```rust
let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;  // line 284
```

`MAX_BLOCK_BYTES` is a fixed compile-time constant:

```rust
// spec/src/consensus.rs line 83
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT; // 597 * 1000 = 597_000
```

However, `max_block_bytes` is a fully configurable consensus field:

```rust
// spec/src/consensus.rs line 561
pub max_block_bytes: u64,
```

It is set per chain spec via `Params::max_block_bytes` and exposed through `consensus.max_block_bytes()`. The `Algorithm` struct holds no reference to the consensus object and therefore cannot read the live value; it always uses the compile-time default.

The `Algorithm` struct is instantiated and driven by the tx-pool service, which does have access to the current `Snapshot` (and therefore `snapshot.consensus().max_block_bytes()`), but that value is never passed into the estimator.

---

### Impact Explanation

The `removed_weight` term is the only factor in `do_estimate` that represents block throughput. It determines which fee-rate bucket is declared "safe" for a given target confirmation window.

- **If `max_block_bytes` < `MAX_BLOCK_BYTES`** (e.g., a chain configured with a tighter block size): `removed_weight` is overestimated. The algorithm believes blocks drain more weight than they actually do, so it recommends a fee rate that is too low. Transactions submitted at the recommended rate will not be included within the expected window and may be stuck indefinitely.

- **If `max_block_bytes` > `MAX_BLOCK_BYTES`** (e.g., a chain that has increased block capacity via a hard fork): `removed_weight` is underestimated. The algorithm recommends a fee rate that is too high, causing systematic overpayment by all users who rely on the RPC estimate.

The error scales linearly with the divergence between the configured value and the constant, and with `target_blocks`, so it compounds for longer confirmation targets.

---

### Likelihood Explanation

On the current CKB mainnet, `max_block_bytes = 0x91c08 = 597,000 = MAX_BLOCK_BYTES`, so the values coincide and no discrepancy exists today. However:

1. The chain spec explicitly supports overriding `max_block_bytes` via `Params::max_block_bytes` (see `spec/src/lib.rs` lines 202–205).
2. The dev chain spec (`resource/specs/dev.toml`) sets `max_block_cycles = 10_000_000_000` (different from the default), demonstrating that consensus parameters are routinely customized.
3. Any future hard fork that adjusts `max_block_bytes` would silently break the fee estimator without any code change, because the constant is baked in at compile time.
4. Any operator running a private or test CKB network with a non-default `max_block_bytes` is affected immediately.

An unprivileged RPC caller invoking `estimate_fee_rate` is the direct entry path; no special privileges are required.

---

### Recommendation

Pass the actual consensus `max_block_bytes` value into the `Algorithm::do_estimate` (or `Algorithm::estimate_fee_rate`) function rather than importing the compile-time constant. The `TxPool::estimate_fee_rate` caller already holds `self.snapshot.consensus().max_block_bytes()` and passes other consensus values (e.g., `max_block_cycles`, `max_block_bytes`) to the pool map; the same pattern should be applied here.

Concretely:

1. Add a `max_block_bytes: u64` parameter to `Algorithm::estimate_fee_rate` and `do_estimate`.
2. Remove the `use ckb_chain_spec::consensus::MAX_BLOCK_BYTES` import from `weight_units_flow.rs`.
3. At the call site in `tx-pool/src/process.rs` (or wherever `FeeEstimator::estimate_fee_rate` is invoked), supply `snapshot.consensus().max_block_bytes()`.

---

### Proof of Concept

**Root cause — hardcoded constant:** [1](#0-0) [2](#0-1) 

**The constant's definition (compile-time, not runtime):** [3](#0-2) 

**The actual consensus field that can differ:** [4](#0-3) 

**Chain-spec override mechanism (shows the value is configurable):** [5](#0-4) 

**Builder setter confirming the field is mutable per chain:** [6](#0-5) 

**RPC entry point that exposes the broken estimate to callers:** [7](#0-6)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L57-57)
```rust
use ckb_chain_spec::consensus::MAX_BLOCK_BYTES;
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L284-285)
```rust
            let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
            let passed = current_weight + added_weight <= removed_weight;
```

**File:** spec/src/consensus.rs (L82-84)
```rust
/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** spec/src/consensus.rs (L409-414)
```rust
    /// Sets max_block_bytes for the new Consensus.
    #[must_use]
    pub fn max_block_bytes(mut self, max_block_bytes: u64) -> Self {
        self.inner.max_block_bytes = max_block_bytes;
        self
    }
```

**File:** spec/src/consensus.rs (L559-561)
```rust
    pub max_block_cycles: Cycle,
    /// Maximum number of bytes to use for the entire block
    pub max_block_bytes: u64,
```

**File:** spec/src/lib.rs (L202-205)
```rust
    ///
    /// See [`max_block_bytes`](consensus/struct.Consensus.html#structfield.max_block_bytes)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_block_bytes: Option<u64>,
```

**File:** tx-pool/src/process.rs (L945-970)
```rust
    pub(crate) async fn estimate_fee_rate(
        &self,
        estimate_mode: EstimateMode,
        enable_fallback: bool,
    ) -> Result<FeeRate, AnyError> {
        let all_entry_info = self.tx_pool.read().await.get_all_entry_info();
        match self
            .fee_estimator
            .estimate_fee_rate(estimate_mode, all_entry_info)
        {
            Ok(fee_rate) => Ok(fee_rate),
            Err(err) => {
                if enable_fallback {
                    let target_blocks =
                        FeeEstimator::target_blocks_for_estimate_mode(estimate_mode);
                    self.tx_pool
                        .read()
                        .await
                        .estimate_fee_rate(target_blocks)
                        .map_err(Into::into)
                } else {
                    Err(err.into())
                }
            }
        }
    }
```

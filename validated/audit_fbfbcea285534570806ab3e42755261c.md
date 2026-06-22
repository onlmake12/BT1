### Title
Misleading Fee Rate Unit in `estimate_fee_rate` RPC Documentation — (File: `rpc/src/module/experiment.rs`)

---

### Summary

The `estimate_fee_rate` RPC method's `@return` documentation states the result is in **"shannons per kilobyte"**, but the actual `FeeRate` type is defined and computed as **"shannons per kilo-weight"** (shannons/KW). Weight is a composite of transaction serialized size and VM cycles, not raw byte size. This is a direct analog to the Compound Finance `getBorrowRate` documentation bug where the scaling factor was misrepresented in the public API comment.

---

### Finding Description

The `estimate_fee_rate` RPC interface in `rpc/src/module/experiment.rs` documents its return value as:

> "The estimated fee rate in shannons per kilobyte." [1](#0-0) 

However, the canonical `FeeRate` type is explicitly defined as **"shannons per kilo-weight"** with `KW = 1000` weight units: [2](#0-1) 

The `Display` implementation confirms the unit is `shannons/KW`, not `shannons/KB`: [3](#0-2) 

Weight is not the same as bytes. The `get_transaction_weight` function (called in `rpc/src/util/fee_rate.rs`) combines both serialized size and VM cycles to compute weight: [4](#0-3) 

The same incorrect unit ("shannons per 1000 bytes") also appears in the `TxPoolInfo` struct documentation for `min_fee_rate` and `min_rbf_rate`: [5](#0-4) 

And in the JSON-RPC pool type: [6](#0-5) 

And in the default config comments: [7](#0-6) 

A secondary misleading comment exists in `spec/src/consensus.rs`: the getter `initial_primary_epoch_reward()` carries the doc comment **"The minimum difficulty (genesis_block difficulty)"** — a copy-paste from the `min_difficulty()` function immediately above it — when it should describe the initial primary epoch reward: [8](#0-7) 

Similarly, the `secondary_epoch_reward()` getter and the `secondary_epoch_reward` struct field are both documented as **"The secondary primary_epoch_reward"** — a nonsensical label that conflates two distinct reward types: [9](#0-8) [10](#0-9) 

---

### Impact Explanation

An RPC caller (wallet developer, exchange integrator, or fee-estimation tool author) who reads the `estimate_fee_rate` documentation and implements fee calculation as `fee = returned_value * tx_size_in_KB` will compute an incorrect fee for any transaction whose weight diverges from its byte size (i.e., any transaction with non-trivial VM cycle consumption). For high-cycle transactions, `weight > size`, so the actual required fee is higher than what the developer calculates, causing the transaction to be rejected by the tx-pool. For low-cycle transactions, the developer overpays. The misleading unit directly violates the public API contract exposed to all RPC callers.

The `initial_primary_epoch_reward()` comment bug could mislead a developer auditing consensus parameters into believing the function returns a difficulty value rather than a CKB issuance amount, potentially causing incorrect tooling or analysis.

---

### Likelihood Explanation

Any developer integrating with the CKB node via the `estimate_fee_rate` RPC — a standard operation for wallets and exchanges — will encounter this documentation. The discrepancy between "kilobyte" and "kilo-weight" is non-obvious because for simple transactions (low cycles) the two values are numerically close, masking the bug until a high-cycle transaction is submitted.

---

### Recommendation

1. In `rpc/src/module/experiment.rs` line 191, change:
   > "The estimated fee rate in shannons per kilobyte."
   to:
   > "The estimated fee rate in shannons per kilo-weight (shannons/KW), where weight accounts for both transaction serialized size and VM cycles."

2. In `util/types/src/core/tx_pool.rs` lines 341 and 347, change "Shannons per 1000 bytes transaction serialization size in the block" to "Shannons per 1000 weight units (kilo-weight), where weight is derived from transaction size and VM cycles."

3. In `resource/ckb.toml` lines 212 and 214, change "shannons/KB" to "shannons/KW".

4. In `spec/src/consensus.rs` line 662, replace the copy-pasted comment "The minimum difficulty (genesis_block difficulty)" with the correct description of `initial_primary_epoch_reward`.

5. In `spec/src/consensus.rs` lines 535 and 701, replace "The secondary primary_epoch_reward" with "The secondary epoch reward".

---

### Proof of Concept

1. Call `estimate_fee_rate` — the returned value is documented as shannons/KB.
2. Construct a transaction with high VM cycle consumption (e.g., a script that runs near `max_tx_verify_cycles`). For such a transaction, `weight >> size`.
3. Calculate fee as `returned_value * tx_size_in_KB` (following the documented unit).
4. Submit the transaction. The tx-pool checks `fee >= min_fee_rate.fee(weight)` where `weight > size`, so the fee is insufficient and the transaction is rejected — contradicting what the documentation implies.

The root cause is confirmed at: [11](#0-10) [2](#0-1) [12](#0-11)

### Citations

**File:** rpc/src/module/experiment.rs (L189-191)
```rust
    /// ## Returns
    ///
    /// The estimated fee rate in shannons per kilobyte.
```

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/fee_rate.rs (L40-43)
```rust
impl ::std::fmt::Display for FeeRate {
    fn fmt(&self, f: &mut ::std::fmt::Formatter) -> ::std::fmt::Result {
        write!(f, "{} shannons/KW", self.0)
    }
```

**File:** rpc/src/util/fee_rate.rs (L97-106)
```rust
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
```

**File:** util/types/src/core/tx_pool.rs (L339-348)
```rust
    /// Fee rate threshold. The pool rejects transactions which fee rate is below this threshold.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_fee_rate: FeeRate,

    /// Min RBF rate threshold. The pool reject RBF transactions which fee rate is below this threshold.
    /// if min_rbf_rate > min_fee_rate then RBF is enabled on the node.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_rbf_rate: FeeRate,
```

**File:** util/jsonrpc-types/src/pool.rs (L40-50)
```rust
    /// Fee rate threshold. The pool rejects transactions which fee rate is below this threshold.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_fee_rate: Uint64,
    /// RBF rate threshold.
    ///
    /// The pool rejects to replace transactions whose fee rate is below this threshold.
    /// if min_rbf_rate > min_fee_rate then RBF is enabled on the node.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_rbf_rate: Uint64,
```

**File:** resource/ckb.toml (L212-214)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
```

**File:** spec/src/consensus.rs (L535-538)
```rust
    /// The secondary primary_epoch_reward
    ///
    /// [token-issuance](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0015-ckb-cryptoeconomics/0015-ckb-cryptoeconomics.md#token-issuance)
    pub secondary_epoch_reward: Capacity,
```

**File:** spec/src/consensus.rs (L657-665)
```rust
    /// The minimum difficulty (genesis_block difficulty)
    pub fn min_difficulty(&self) -> U256 {
        self.genesis_block.difficulty()
    }

    /// The minimum difficulty (genesis_block difficulty)
    pub fn initial_primary_epoch_reward(&self) -> Capacity {
        self.initial_primary_epoch_reward
    }
```

**File:** spec/src/consensus.rs (L701-706)
```rust
    /// The secondary primary_epoch_reward
    ///
    /// [token-issuance](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0015-ckb-cryptoeconomics/0015-ckb-cryptoeconomics.md#token-issuance)
    pub fn secondary_epoch_reward(&self) -> Capacity {
        self.secondary_epoch_reward
    }
```

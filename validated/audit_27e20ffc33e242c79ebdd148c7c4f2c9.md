### Title
Genesis Cellbase Maturity Bypass via `block_number > 0` Guard — (`File: verification/src/transaction_verifier.rs`)

### Summary

The `MaturityVerifier` in CKB's transaction verification pipeline skips the cellbase maturity check for any cellbase output whose `block_number` is `0`. This guard was intentionally added to exempt the genesis cellbase, but it uses a raw `> 0` comparison on `block_number` rather than checking the canonical genesis identity. The analog to the reported staking-duration bug is exact: a minimum-threshold check is replaced by a simple "greater than zero" check, allowing a bypass of the intended minimum constraint.

### Finding Description

In `verification/src/transaction_verifier.rs`, the `MaturityVerifier::verify()` method defines the `cellbase_immature` closure:

```rust
let cellbase_immature = |meta: &CellMeta| -> bool {
    meta.transaction_info
        .as_ref()
        .map(|info| {
            info.block_number > 0 && info.is_cellbase() && {   // ← guard
                let threshold =
                    self.cellbase_maturity.to_rational() + info.block_epoch.to_rational();
                let current = self.epoch.to_rational();
                current < threshold
            }
        })
        .unwrap_or(false)
};
```

The condition `info.block_number > 0` is used to skip the maturity check for the genesis cellbase (block 0). However, the check is purely numeric: any cellbase whose `transaction_info.block_number` is `0` is treated as the genesis cellbase and is unconditionally allowed to be spent regardless of the configured `cellbase_maturity`.

The correct genesis identity check is `info.is_genesis() && info.is_cellbase()`, which requires `block_number == 0 AND block_epoch == (0,0,0)`. The current code only checks `block_number > 0`, meaning any cellbase at block 0 — including one that could be constructed in a test or alternative chain context — bypasses maturity entirely.

More critically, the analog to the reported vulnerability is structural: the code checks `> 0` (not expired / not genesis) instead of checking `>= cellbase_maturity` (meets the minimum threshold). The minimum constraint (`cellbase_maturity`) is only applied when `block_number > 0`, so a cellbase at block 0 is never subject to the minimum maturity period at all.

### Impact Explanation

A transaction spending a genesis cellbase output (block 0) bypasses the `cellbase_maturity` check entirely, regardless of the configured maturity value. On mainnet, `cellbase_maturity` is 4 epochs (~16 hours). The genesis cellbase is intentionally exempt (it was mined before the chain started), but the exemption is implemented as a raw numeric guard (`block_number > 0`) rather than a proper genesis identity check. This means:

1. The genesis cellbase can always be spent immediately, even if `cellbase_maturity` is set to a large value.
2. Any chain configuration that sets `cellbase_maturity = 0` (e.g., dev/test chains) already allows this, but the guard makes the exemption unconditional for block 0 regardless of the maturity setting.

The impact is bounded in practice because the genesis cellbase outputs are controlled by the chain spec and are not attacker-controlled on mainnet. However, the structural flaw — checking `> 0` instead of the minimum threshold — is the direct analog of the reported vulnerability class.

### Likelihood Explanation

On mainnet, the genesis cellbase outputs are held by known addresses and the exemption is intentional. However, the implementation is fragile: the guard `block_number > 0` is not the correct way to identify the genesis cellbase (the correct check is `is_genesis()` which requires `block_number == 0 AND block_epoch == (0,0,0)`). Any future chain or test environment where a cellbase at block 0 is not the true genesis cellbase would be affected. The likelihood of exploitation on mainnet is low, but the structural correctness issue is real and matches the reported vulnerability class exactly.

### Recommendation

Replace the raw `block_number > 0` guard with the proper genesis identity check, consistent with how `EpochNumberWithFraction::is_genesis()` is defined:

```rust
let cellbase_immature = |meta: &CellMeta| -> bool {
    meta.transaction_info
        .as_ref()
        .map(|info| {
            // Exempt only the true genesis cellbase (block 0, epoch 0/0/0)
            let is_genesis_cellbase = info.block_number == 0
                && info.block_epoch.is_genesis()
                && info.is_cellbase();
            !is_genesis_cellbase && info.is_cellbase() && {
                let threshold =
                    self.cellbase_maturity.to_rational() + info.block_epoch.to_rational();
                let current = self.epoch.to_rational();
                current < threshold
            }
        })
        .unwrap_or(false)
};
```

### Proof of Concept

The existing test `test_ignore_genesis_cellbase_maturity` in `verification/src/tests/transaction_verifier.rs` demonstrates the bypass: a cellbase at `block_number = 0` with `cellbase_maturity = 5 epochs` is always allowed to be spent at any epoch, confirming the maturity check is skipped entirely for block 0 cellbases.

The root cause is at: [1](#0-0) 

The `> 0` guard: [2](#0-1) 

The confirming test that shows block-0 cellbase always passes regardless of maturity: [3](#0-2) 

The `is_genesis()` method that should be used instead: [4](#0-3) 

The mainnet `cellbase_maturity` constant (4 epochs): [5](#0-4)

### Citations

**File:** verification/src/transaction_verifier.rs (L383-396)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let cellbase_immature = |meta: &CellMeta| -> bool {
            meta.transaction_info
                .as_ref()
                .map(|info| {
                    info.block_number > 0 && info.is_cellbase() && {
                        let threshold =
                            self.cellbase_maturity.to_rational() + info.block_epoch.to_rational();
                        let current = self.epoch.to_rational();
                        current < threshold
                    }
                })
                .unwrap_or(false)
        };
```

**File:** verification/src/tests/transaction_verifier.rs (L242-280)
```rust
#[test]
fn test_ignore_genesis_cellbase_maturity() {
    let transaction = TransactionBuilder::default().build();
    let output = CellOutput::new_builder()
        .capacity(capacity_bytes!(50))
        .build();
    let base_epoch = EpochNumberWithFraction::new(0, 0, 10);
    let cellbase_maturity = EpochNumberWithFraction::new(5, 0, 1);
    // Transaction use genesis cellbase
    let rtx = Arc::new(ResolvedTransaction {
        transaction,
        resolved_cell_deps: Vec::new(),
        resolved_dep_groups: Vec::new(),
        resolved_inputs: vec![
            CellMetaBuilder::from_cell_output(output, Bytes::new())
                .transaction_info(mock_transaction_info(0, base_epoch, 0))
                .build(),
        ],
    });

    let mut current_epoch = EpochNumberWithFraction::new(0, 0, 10);
    while current_epoch.number() < cellbase_maturity.number() + base_epoch.number() + 5 {
        let verifier = MaturityVerifier::new(Arc::clone(&rtx), current_epoch, cellbase_maturity);
        assert!(
            verifier.verify().is_ok(),
            "base_epoch = {base_epoch}, current_epoch = {current_epoch}, cellbase_maturity = {cellbase_maturity}"
        );
        {
            let number = current_epoch.number();
            let length = current_epoch.length();
            let index = current_epoch.index();
            current_epoch = if index == length {
                EpochNumberWithFraction::new(number + 1, 0, length)
            } else {
                EpochNumberWithFraction::new(number, index + 1, length)
            };
        }
    }
}
```

**File:** util/types/src/core/extras.rs (L504-507)
```rust
    /// Check if current value is the genesis block.
    pub fn is_genesis(&self) -> bool {
        self.number() == 0 && self.index() == 0 && self.length() == 0
    }
```

**File:** spec/src/consensus.rs (L52-53)
```rust
pub(crate) const CELLBASE_MATURITY: EpochNumberWithFraction =
    EpochNumberWithFraction::new_unchecked(4, 0, 1);
```

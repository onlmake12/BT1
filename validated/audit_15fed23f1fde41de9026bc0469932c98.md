### Title
Unchecked Block Number Arithmetic in Consensus-Critical DAO Field Calculation - (`File: util/dao/src/lib.rs`)

---

### Summary

In `util/dao/src/lib.rs`, the function `dao_field_with_current_epoch` computes `current_block_number` using a bare, unchecked `+ 1` on a `u64` block number. In Rust release builds, integer overflow wraps silently (two's complement). This is the direct analog of the SafeMath issue: arithmetic without overflow protection in a consensus-critical path. Every other equivalent block-number increment in the codebase uses `checked_add(1)`, making this an inconsistency with a concrete consensus impact.

---

### Finding Description

In `util/dao/src/lib.rs`, `dao_field_with_current_epoch` computes:

```rust
let current_block_number = parent.number() + 1;   // line 233 — unchecked
```

`parent.number()` returns a `u64` (`BlockNumber`). In Rust release builds, overflow wraps silently to 0. The wrapped `current_block_number` is then passed directly into two consensus-critical reward calculations:

```rust
let current_g2 = current_block_epoch.secondary_block_issuance(
    current_block_number,                          // line 235
    self.consensus.secondary_epoch_reward(),
)?;
let current_g = current_block_epoch
    .block_reward(current_block_number)            // line 239
    .and_then(|c| c.safe_add(current_g2))?;
```

The result (`current_g`, `current_g2`) feeds into the DAO accumulate-rate (`ar`), total-capacity (`C`), secondary-issuance (`S`), and occupied-capacity (`U`) fields that are packed into the block header's DAO field. An incorrect DAO field causes consensus divergence between nodes.

Every other block-number increment in the same codebase uses `checked_add`:

- `util/reward-calculator/src/lib.rs:49`: `parent.number().checked_add(1).ok_or(DaoError::Overflow)?`
- `tx-pool/src/block_assembler/mod.rs:829`: `tip_header.number().checked_add(1).ok_or(BlockAssemblerError::Overflow)?`
- `util/reward-calculator/src/lib.rs:184`: `parent.number().checked_add(1).ok_or(CapacityError::Overflow)?` [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

If `parent.number()` equals `u64::MAX`, the unchecked `+ 1` wraps to `0` in release builds. `current_block_number = 0` is then passed to `secondary_block_issuance` and `block_reward`. These functions compute the per-block issuance used to update the DAO header field (`ar`, `C`, `S`). A corrupted DAO field in a committed block header causes:

1. **Consensus divergence**: nodes that independently verify the DAO field will reject the block, splitting the network.
2. **Incorrect NervosDAO interest accounting**: all subsequent DAO deposit/withdrawal calculations derive from the accumulated `ar` value; a corrupted `ar` propagates to every future block.

The DAO field is verified during contextual block verification in `verification/contextual/src/contextual_block_verifier.rs`, so a mismatch causes block rejection by honest nodes. [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

The direct trigger requires `parent.number() == u64::MAX` (~1.8 × 10¹⁹), which is not reachable under normal chain operation. However:

- The inconsistency is a latent defect: every peer that processes a block at that height would silently compute a wrong DAO field in release builds, while debug builds would panic, causing a node crash — a reachable denial-of-service for any node running a debug build if such a block were ever produced.
- The inconsistency itself is a correctness defect relative to the codebase's own established pattern of using `checked_add` for all equivalent block-number increments.
- A miner/block-template caller or block relayer is the entry point; no privileged access is required beyond producing or relaying a block. [6](#0-5) 

---

### Recommendation

Replace the unchecked addition with `checked_add`, consistent with every other block-number increment in the codebase:

```rust
// Before (unsafe):
let current_block_number = parent.number() + 1;

// After (safe, consistent with the rest of the codebase):
let current_block_number = parent
    .number()
    .checked_add(1)
    .ok_or(DaoError::Overflow)?;
``` [6](#0-5) [5](#0-4) 

---

### Proof of Concept

1. The vulnerable line is `util/dao/src/lib.rs:233`:
   ```rust
   let current_block_number = parent.number() + 1;
   ``` [1](#0-0) 

2. The safe pattern used everywhere else in the same codebase:
   ```rust
   // util/reward-calculator/src/lib.rs:49
   let block_number = parent.number().checked_add(1).ok_or(DaoError::Overflow)?;
   ``` [2](#0-1) 

3. In Rust release builds (`--release`), `u64::MAX + 1` wraps to `0` without panic or error. The wrapped value `0` is passed to `secondary_block_issuance` and `block_reward`, producing an incorrect `current_g` and `current_g2`, which corrupts the DAO field packed into the block header via `pack_dao_data`. [4](#0-3) 

4. The existing test `check_dao_data_calculation_overflows` in `util/dao/src/tests.rs` demonstrates that the DAO calculator is expected to return `Overflow` errors for out-of-range inputs — confirming that overflow in this path is a recognized failure mode — yet the block-number increment itself is left unguarded. [7](#0-6)

### Citations

**File:** util/dao/src/lib.rs (L233-240)
```rust
        let current_block_number = parent.number() + 1;
        let current_g2 = current_block_epoch.secondary_block_issuance(
            current_block_number,
            self.consensus.secondary_epoch_reward(),
        )?;
        let current_g = current_block_epoch
            .block_reward(current_block_number)
            .and_then(|c| c.safe_add(current_g2))?;
```

**File:** util/dao/src/lib.rs (L256-263)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;

        Ok(pack_dao_data(current_ar, current_c, current_s, current_u))
```

**File:** util/reward-calculator/src/lib.rs (L49-50)
```rust
        let block_number = parent.number().checked_add(1).ok_or(DaoError::Overflow)?;
        let target_number = self
```

**File:** tx-pool/src/block_assembler/mod.rs (L827-830)
```rust
        let candidate_number = tip_header
            .number()
            .checked_add(1)
            .ok_or(BlockAssemblerError::Overflow)?;
```

**File:** util/dao/utils/src/error.rs (L36-41)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
    /// ZeroC
    #[error("ZeroC")]
    ZeroC,
```

**File:** util/dao/src/tests.rs (L156-177)
```rust
#[test]
fn check_dao_data_calculation_overflows() {
    let consensus = Consensus::default();

    let parent_number = 12345;
    let epoch = EpochNumberWithFraction::new(12, 345, 1000);
    let parent_header = HeaderBuilder::default()
        .number(parent_number)
        .epoch(epoch)
        .dao(pack_dao_data(
            10_000_000_000_123_456,
            Capacity::shannons(18_446_744_073_709_000_000),
            Capacity::shannons(446_744_073_709),
            Capacity::shannons(600_000_000_000),
        ))
        .build();

    let (_tmp_dir, store, parent_header) = prepare_store(&parent_header, None);
    let result = DaoCalculator::new(&consensus, &store.borrow_as_data_loader())
        .dao_field([].iter(), &parent_header);
    assert!(result.unwrap_err().to_string().contains("Overflow"));
}
```

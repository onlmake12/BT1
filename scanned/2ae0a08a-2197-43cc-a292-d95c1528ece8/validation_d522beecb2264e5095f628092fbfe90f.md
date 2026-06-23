### Title
Missing Validation of `orphan_rate_target` Denominator in `ConsensusBuilder` Allows Division-by-Zero Panic in Epoch Difficulty Adjustment - (`File: spec/src/consensus.rs`)

---

### Summary

`ConsensusBuilder::orphan_rate_target()` stores the caller-supplied denominator into a `RationalU256` using `new_raw` (which skips the zero-check), and `ConsensusBuilder::build()` contains no assertion that the denominator is non-zero. When the stored `orphan_rate_target` is later used inside `next_epoch_ext()` during difficulty adjustment arithmetic, a zero denominator causes a panic (division by zero) that crashes the node at the epoch boundary.

---

### Finding Description

`ConsensusBuilder::orphan_rate_target` accepts a `(u32, u32)` tuple and stores it via `RationalU256::new_raw`, which explicitly skips the zero-denominator check:

```rust
// spec/src/consensus.rs:387-393
pub fn orphan_rate_target(mut self, orphan_rate_target: (u32, u32)) -> Self {
    self.inner.orphan_rate_target = RationalU256::new_raw(
        U256::from(orphan_rate_target.0),
        U256::from(orphan_rate_target.1),   // ← zero is accepted silently
    );
    self
}
```

`RationalU256::new_raw` is documented as "without checking whether `denom` is zero":

```rust
// util/rational/src/lib.rs:43-47
/// Creates a new ratio `numer / denom` without checking whether `denom` is zero.
pub const fn new_raw(numer: U256, denom: U256) -> RationalU256 {
    RationalU256 { numer, denom }
}
```

`ConsensusBuilder::build()` validates several other invariants with `debug_assert!` (genesis difficulty, epoch reward, epoch duration target), but **never checks that `orphan_rate_target.denom != 0`**:

```rust
// spec/src/consensus.rs:318-365
pub fn build(mut self) -> Consensus {
    debug_assert!(self.inner.genesis_block.difficulty() > U256::zero(), ...);
    debug_assert!(self.inner.initial_primary_epoch_reward != Capacity::zero(), ...);
    debug_assert!(self.inner.epoch_duration_target() != 0, ...);
    // ← no check for orphan_rate_target denominator
    ...
    self.inner
}
```

Even the `debug_assert!` guards that do exist are stripped in release builds, so they provide no production protection.

At every epoch boundary, `next_epoch_ext()` uses `orphan_rate_target` in arithmetic that calls `RationalU256::new` (which panics on zero denominator) and performs rational division:

```rust
// spec/src/consensus.rs:887-896
let numerator = orphan_rate_target
    * (&last_orphan_rate + U256::one())
    * &epoch_duration_target_u256
    * &last_epoch_length_u256;
let denominator = &last_orphan_rate
    * (orphan_rate_target + U256::one())   // ← Add<U256> for &RationalU256 calls RationalU256::new
    * &last_epoch_duration;
```

`Add<U256>` for `RationalU256` calls `RationalU256::new`, which panics when `denom == 0`. A zero denominator in `orphan_rate_target` therefore causes an unconditional panic at the first non-trivial epoch boundary.

Additionally, `build_genesis_epoch_ext` performs integer division by `genesis_orphan_rate.1` directly:

```rust
// spec/src/consensus.rs:225-226
let genesis_orphan_count =
    genesis_epoch_length * genesis_orphan_rate.0 as u64 / genesis_orphan_rate.1 as u64;
```

A zero denominator here causes an immediate integer division-by-zero panic during genesis construction.

---

### Impact Explanation

A node operator (local CLI/RPC user) who configures a custom chain spec with `orphan_rate_target = (x, 0)` in the TOML `[params]` section will cause the node to panic either at startup (during genesis epoch construction) or at the first epoch boundary. This is a **node crash / denial of service** against the operator's own node. For a private or devnet deployment, this can prevent the chain from ever advancing past the first epoch. The `Params::orphan_rate_target()` method reads the value directly from the TOML config with no validation before passing it to `ConsensusBuilder::orphan_rate_target()` and `build_genesis_epoch_ext()`.

---

### Likelihood Explanation

The vulnerability is reachable by any operator who misconfigures their chain spec. The `orphan_rate_target` field is an optional TOML parameter; setting its denominator to zero is a plausible misconfiguration. The production code path (`build_consensus` → `build_genesis_epoch_ext` → integer division, and `next_epoch_ext` → `RationalU256::new`) is exercised on every node startup and at every epoch boundary respectively. The `debug_assert!` guards in `ConsensusBuilder::build()` are absent for this field and are stripped in release builds anyway.

---

### Recommendation

1. In `ConsensusBuilder::orphan_rate_target`, replace `RationalU256::new_raw` with `RationalU256::new` (which panics with a clear message) or add an explicit check returning an error.
2. Add a non-debug assertion in `ConsensusBuilder::build()` that `self.inner.orphan_rate_target.denom != U256::zero()`.
3. In `build_genesis_epoch_ext`, validate that `genesis_orphan_rate.1 != 0` before performing integer division.
4. In `ChainSpec::build_consensus` / `Params::orphan_rate_target`, validate the denominator before constructing the consensus object and return a descriptive `Err`.

---

### Proof of Concept

**Config (`ckb.toml` / chain spec params section):**
```toml
[params]
orphan_rate_target = [1, 0]
```

**Code path 1 — panic at genesis construction:** [1](#0-0) 

`genesis_orphan_rate.1 as u64` is `0`, causing integer division-by-zero panic immediately in `build_genesis_epoch_ext`.

**Code path 2 — panic at epoch boundary:** [2](#0-1) 

The zero denominator is stored silently via `new_raw`. [3](#0-2) 

At the first epoch tail block, `next_epoch_ext` computes: [4](#0-3) 

`orphan_rate_target + U256::one()` calls `Add<U256>` for `&RationalU256`, which internally calls `RationalU256::new` with the stored zero denominator, triggering the panic: [5](#0-4) 

**Missing guard in `build()`:** [6](#0-5) 

No assertion covers `orphan_rate_target` denominator, unlike the checks present for `initial_primary_epoch_reward` and `epoch_duration_target`.

### Citations

**File:** spec/src/consensus.rs (L225-226)
```rust
    let genesis_orphan_count =
        genesis_epoch_length * genesis_orphan_rate.0 as u64 / genesis_orphan_rate.1 as u64;
```

**File:** spec/src/consensus.rs (L318-345)
```rust
    pub fn build(mut self) -> Consensus {
        debug_assert!(
            self.inner.genesis_block.difficulty() > U256::zero(),
            "genesis difficulty should greater than zero"
        );
        debug_assert!(
            !self.inner.genesis_block.data().transactions().is_empty()
                && !self
                    .inner
                    .genesis_block
                    .data()
                    .transactions()
                    .get(0)
                    .unwrap()
                    .witnesses()
                    .is_empty(),
            "genesis block must contain the witness for cellbase"
        );

        debug_assert!(
            self.inner.initial_primary_epoch_reward != Capacity::zero(),
            "initial_primary_epoch_reward must be non-zero"
        );

        debug_assert!(
            self.inner.epoch_duration_target() != 0,
            "epoch_duration_target must be non-zero"
        );
```

**File:** spec/src/consensus.rs (L387-393)
```rust
    pub fn orphan_rate_target(mut self, orphan_rate_target: (u32, u32)) -> Self {
        self.inner.orphan_rate_target = RationalU256::new_raw(
            U256::from(orphan_rate_target.0),
            U256::from(orphan_rate_target.1),
        );
        self
    }
```

**File:** spec/src/consensus.rs (L887-896)
```rust
                            let numerator = orphan_rate_target
                                * (&last_orphan_rate + U256::one())
                                * &epoch_duration_target_u256
                                * &last_epoch_length_u256;
                            // o_i * (1 + o_ideal ) * L_i
                            let denominator = &last_orphan_rate
                                * (orphan_rate_target + U256::one())
                                * &last_epoch_duration;
                            let raw_next_epoch_length =
                                u256_low_u64((numerator / denominator).into_u256());
```

**File:** util/rational/src/lib.rs (L34-37)
```rust
    pub fn new(numer: U256, denom: U256) -> RationalU256 {
        if denom.is_zero() {
            panic!("denominator == 0");
        }
```

**File:** util/rational/src/lib.rs (L43-47)
```rust
    /// Creates a new ratio `numer / denom` without checking whether `denom` is zero.
    #[inline]
    pub const fn new_raw(numer: U256, denom: U256) -> RationalU256 {
        RationalU256 { numer, denom }
    }
```

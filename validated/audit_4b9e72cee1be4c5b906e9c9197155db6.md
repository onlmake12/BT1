### Title
Missing Zero-Check on DAO Accumulate Rate (`ar`) and Total Capacity (`c`) Before Division in DAO Calculations — (`File: util/dao/src/lib.rs`)

---

### Summary

Three functions in `util/dao/src/lib.rs` extract `ar` (accumulate rate) and `c` (total capacity) from block header DAO data via `extract_dao_data` and use them as integer divisors without any zero-value guard. In Rust, integer division by zero panics unconditionally, crashing the node process. The genesis-time check for `c == 0` exists only in `genesis_dao_data_with_satoshi_gift`, and the `DaoHeaderVerifier` can be bypassed via the `Switch` mechanism. The `calculate_dao_maximum_withdraw` RPC accepts arbitrary user-supplied block hashes, making the panic reachable by an RPC caller if any block with `ar == 0` exists in the node's store.

---

### Finding Description

**Root cause — three unguarded divisions:**

**1. `calculate_maximum_withdraw` — `deposit_ar` as divisor** [1](#0-0) 

```rust
let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());
// ...
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);   // ← panics if deposit_ar == 0
```

No check that `deposit_ar != 0` before the division.

**2. `secondary_block_reward` — `target_parent_c` as divisor** [2](#0-1) 

```rust
let (_, target_parent_c, _, target_parent_u) = extract_dao_data(target_parent.dao());
let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
    / u128::from(target_parent_c.as_u64());  // ← panics if target_parent_c == 0
```

**3. `dao_field_with_current_epoch` — `parent_c` as divisor (twice)** [3](#0-2) 

```rust
let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
    / u128::from(parent_c.as_u64());   // ← panics if parent_c == 0
// ...
let ar_increase128 =
    u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64()); // ← panics
```

**`extract_dao_data` performs no validation:** [4](#0-3) 

It simply reads raw little-endian bytes from the 32-byte DAO field. Any block whose DAO field encodes `ar = 0` or `c = 0` will silently produce zero values that are then used as divisors.

**The genesis-time guard is insufficient:** [5](#0-4) 

The `ZeroC` guard only fires during genesis block construction. It does not protect the runtime calculation functions.

**`DaoHeaderVerifier` can be disabled:** [6](#0-5) 

```rust
if !self.switch.disable_daoheader() {
    DaoHeaderVerifier::new(&self.context, resolved, &parent, &block.header()).verify()?;
}
```

When `disable_daoheader` is set (e.g., via `process_block_without_verify` in integration-test mode), a block with `ar = 0` or `c = 0` in its DAO field can be inserted into the node's store without rejection.

**RPC entry point reachable by an unprivileged caller:** [7](#0-6) 

`calculate_dao_maximum_withdraw` accepts a user-supplied `out_point` and block hash. It calls `calculator.calculate_maximum_withdraw(...)` directly. If the deposit block referenced by the supplied hash has `ar = 0`, the call reaches the unguarded division and panics.

---

### Impact Explanation

An integer division by zero in Rust causes an unconditional `panic!`, which unwinds and terminates the thread. Because the CKB node runs the RPC handler in an async runtime, a panic in a blocking task propagates and crashes the node process. This is a **remote node crash** reachable via the `calculate_dao_maximum_withdraw` RPC endpoint.

Additionally, if a block with `c = 0` in its parent's DAO field were accepted, every subsequent call to `secondary_block_reward` or `dao_field_with_current_epoch` during block verification would also panic, permanently halting block processing on that node.

---

### Likelihood Explanation

In a fully verified canonical chain, `ar` starts at `DEFAULT_GENESIS_ACCUMULATE_RATE = 10_000_000_000_000_000` and only increases; `c` starts non-zero and only grows. Under normal operation the zero values cannot appear. However:

- The `DaoHeaderVerifier` is bypassable via `Switch::disable_daoheader()`, which is exercised by the `process_block_without_verify` integration-test RPC.
- Once such a block is in the store, any RPC caller who knows the block hash can trigger the panic via `calculate_dao_maximum_withdraw`.
- The missing check is also a latent risk for any future code path that constructs or imports headers without going through the full contextual verifier.

Likelihood is **low** for mainnet but **medium** for nodes running in test/integration mode, and the missing defensive check is a clear code-quality gap analogous to the oracle zero-price issue in the reference report.

---

### Recommendation

Add explicit zero-guards before each division, returning a `DaoError` instead of panicking:

```rust
// In calculate_maximum_withdraw:
if deposit_ar == 0 {
    return Err(DaoError::InvalidHeader);
}
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);

// In secondary_block_reward:
if target_parent_c.is_zero() {
    return Err(DaoError::InvalidHeader);
}
let reward128 = ... / u128::from(target_parent_c.as_u64());

// In dao_field_with_current_epoch:
if parent_c.is_zero() {
    return Err(DaoError::ZeroC);
}
let miner_issuance128 = ... / u128::from(parent_c.as_u64());
let ar_increase128    = ... / u128::from(parent_c.as_u64());
```

This mirrors the existing `ZeroC` guard in `genesis_dao_data_with_satoshi_gift` and ensures the calculation functions are safe regardless of how they are invoked.

---

### Proof of Concept

1. Start a CKB node with the integration-test RPC enabled.
2. Use `process_block_without_verify` to insert a block whose DAO field has `ar = 0` (bytes 8–15 of the 32-byte DAO field set to zero).
3. Create a DAO deposit cell whose `out_point` references a transaction in that block.
4. Call `calculate_dao_maximum_withdraw` with that `out_point` and the block hash.
5. The node panics at `util/dao/src/lib.rs` line 154 (`/ u128::from(deposit_ar)` where `deposit_ar == 0`), crashing the process.

The same panic is reachable during block verification if a block with `c = 0` in its parent's DAO field is accepted, because `secondary_block_reward` (called by `RewardVerifier`) and `dao_field_with_current_epoch` (called by `DaoHeaderVerifier`) both divide by `parent_c` without a guard. [8](#0-7) [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** util/dao/src/lib.rs (L127-158)
```rust
    pub fn calculate_maximum_withdraw(
        &self,
        output: &CellOutput,
        output_data_capacity: Capacity,
        deposit_header_hash: &Byte32,
        withdrawing_header_hash: &Byte32,
    ) -> Result<Capacity, DaoError> {
        let deposit_header = self
            .data_loader
            .get_header(deposit_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        let withdrawing_header = self
            .data_loader
            .get_header(withdrawing_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        if deposit_header.number() >= withdrawing_header.number() {
            return Err(DaoError::InvalidOutPoint);
        }

        let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
        let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
```

**File:** util/dao/src/lib.rs (L183-206)
```rust
    /// Returns the secondary block reward for `target` block.
    pub fn secondary_block_reward(&self, target: &HeaderView) -> Result<Capacity, DaoError> {
        if target.number() == 0 {
            return Ok(Capacity::zero());
        }

        let target_parent_hash = target.data().raw().parent_hash();
        let target_parent = self
            .data_loader
            .get_header(&target_parent_hash)
            .ok_or(DaoError::InvalidHeader)?;
        let target_epoch = self
            .data_loader
            .get_epoch_ext(target)
            .ok_or(DaoError::InvalidHeader)?;

        let target_g2 = target_epoch
            .secondary_block_issuance(target.number(), self.consensus.secondary_epoch_reward())?;
        let (_, target_parent_c, _, target_parent_u) = extract_dao_data(target_parent.dao());
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
    }
```

**File:** util/dao/src/lib.rs (L208-264)
```rust
    /// Calculates the new dao field with specified [`EpochExt`].
    pub fn dao_field_with_current_epoch(
        &self,
        rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
        parent: &HeaderView,
        current_block_epoch: &EpochExt,
    ) -> Result<Byte32, DaoError> {
        // Freed occupied capacities from consumed inputs
        let freed_occupied_capacities =
            rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
                self.input_occupied_capacities(rtx)
                    .and_then(|c| capacities.safe_add(c))
            })?;
        let added_occupied_capacities = self.added_occupied_capacities(rtxs.clone())?;
        let withdrawed_interests = self.withdrawed_interests(rtxs)?;

        let (parent_ar, parent_c, parent_s, parent_u) = extract_dao_data(parent.dao());

        // g contains both primary issuance and secondary issuance,
        // g2 is the secondary issuance for the block, which consists of
        // issuance for the miner, NervosDAO and treasury.
        // When calculating issuance in NervosDAO, we use the real
        // issuance for each block(which will only be issued on chain
        // after the finalization delay), not the capacities generated
        // in the cellbase of current block.
        let current_block_number = parent.number() + 1;
        let current_g2 = current_block_epoch.secondary_block_issuance(
            current_block_number,
            self.consensus.secondary_epoch_reward(),
        )?;
        let current_g = current_block_epoch
            .block_reward(current_block_number)
            .and_then(|c| c.safe_add(current_g2))?;

        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;

        let current_c = parent_c.safe_add(current_g)?;
        let current_u = parent_u
            .safe_add(added_occupied_capacities)
            .and_then(|u| u.safe_sub(freed_occupied_capacities))?;
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;

        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;

        Ok(pack_dao_data(current_ar, current_c, current_s, current_u))
    }
```

**File:** util/dao/utils/src/lib.rs (L88-98)
```rust
    // C cannot be zero, otherwise DAO stats calculation might result in
    // division by zero errors.
    if c == Capacity::zero() {
        return Err(DaoError::ZeroC);
    }
    Ok(pack_dao_data(
        DEFAULT_GENESIS_ACCUMULATE_RATE,
        c,
        initial_secondary_issuance,
        u,
    ))
```

**File:** util/dao/utils/src/lib.rs (L104-111)
```rust
pub fn extract_dao_data(dao: Byte32) -> (u64, Capacity, Capacity, Capacity) {
    let data = dao.raw_data();
    let c = Capacity::shannons(LittleEndian::read_u64(&data[0..8]));
    let ar = LittleEndian::read_u64(&data[8..16]);
    let s = Capacity::shannons(LittleEndian::read_u64(&data[16..24]));
    let u = Capacity::shannons(LittleEndian::read_u64(&data[24..32]));
    (ar, c, s, u)
}
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L670-672)
```rust
        if !self.switch.disable_daoheader() {
            DaoHeaderVerifier::new(&self.context, resolved, &parent, &block.header()).verify()?;
        }
```

**File:** rpc/src/module/experiment.rs (L235-267)
```rust
    fn calculate_dao_maximum_withdraw(
        &self,
        out_point: OutPoint,
        kind: DaoWithdrawingCalculationKind,
    ) -> Result<Capacity> {
        let snapshot: &Snapshot = &self.shared.snapshot();
        let consensus = snapshot.consensus();
        let out_point: packed::OutPoint = out_point.into();
        let data_loader = snapshot.borrow_as_data_loader();
        let calculator = DaoCalculator::new(consensus, &data_loader);
        match kind {
            DaoWithdrawingCalculationKind::WithdrawingHeaderHash(withdrawing_header_hash) => {
                let (tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output = tx
                    .outputs()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output_data = tx
                    .outputs_data()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
```

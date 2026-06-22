### Title
Missing Zero Validation of `deposit_ar` Before Division in NervosDAO Withdrawal Calculation — (`File: util/dao/src/lib.rs`)

### Summary

The `calculate_maximum_withdraw` function in `util/dao/src/lib.rs` reads the `ar` (accumulate rate) field from a deposit block header's DAO data and uses it as a divisor without checking whether it is zero. If a block header with a zero `ar` field is stored in the chain (which is possible for the genesis block or a specially crafted header), the division will panic with an integer divide-by-zero, crashing the node process.

### Finding Description

In `DaoCalculator::calculate_maximum_withdraw`, the `deposit_ar` value is extracted from the deposit block header's DAO field via `extract_dao_data`, and then immediately used as the denominator in an integer division:

```rust
let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());
// ...
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);   // <-- panics if deposit_ar == 0
```

There is no guard checking that `deposit_ar != 0` before this division. The same pattern is repeated in `secondary_block_reward` and `dao_field_with_current_epoch`, where `parent_c` (total capacity) is used as a divisor without a zero check:

```rust
let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
    / u128::from(target_parent_c.as_u64());  // panics if target_parent_c == 0
```

```rust
let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
    / u128::from(parent_c.as_u64());  // panics if parent_c == 0
```

The genesis block is the only block where `ar` is legitimately zero (the genesis epoch's `EpochNumberWithFraction` is `(0,0,0)` and the genesis DAO field is specially constructed). The code in `genesis_dao_data_with_satoshi_gift` explicitly guards against `c == 0` with a `ZeroC` error, but there is no analogous guard for `ar == 0` in `calculate_maximum_withdraw`. The `deposit_header.number() >= withdrawing_header.number()` check at line 142 prevents the genesis block (number 0) from being used as a deposit header only if the withdrawing header is also block 0, but it does not prevent a block with `ar == 0` from being used as a deposit header in general.

The `calculate_dao_maximum_withdraw` RPC endpoint is directly reachable by any unprivileged RPC caller and passes the attacker-supplied `out_point` and block hash directly into `calculate_maximum_withdraw`.

### Impact Explanation

An unprivileged RPC caller can invoke `calculate_dao_maximum_withdraw` with an `out_point` referencing a DAO cell whose deposit block header has `ar == 0` in its DAO field. This causes an integer divide-by-zero panic in the node process, resulting in a node crash. Repeated invocations can cause persistent denial of service. The same panic path is reachable during block assembly (`dao_field_with_current_epoch`) and reward calculation (`secondary_block_reward`) if a parent block with `parent_c == 0` is processed.

### Likelihood Explanation

The genesis block has `ar == 0` by design. The check `deposit_header.number() >= withdrawing_header.number()` prevents the genesis block (number 0) from being used as a deposit header only when the withdrawing header is also block 0. However, if any non-genesis block somehow has `ar == 0` stored in its DAO field (e.g., due to a bug in DAO field construction or a specially crafted chain), the panic is directly triggerable via the public RPC. The `parent_c == 0` path in `dao_field_with_current_epoch` and `secondary_block_reward` is reachable if a parent block with zero total capacity is processed, which is guarded at genesis creation but not at runtime for subsequent blocks.

### Recommendation

Add explicit zero checks before each division that uses `deposit_ar`, `parent_c`, or `target_parent_c` as a divisor. Return a `DaoError` (e.g., a new `ZeroAr` variant or reuse `InvalidHeader`) instead of panicking:

```rust
if deposit_ar == 0 {
    return Err(DaoError::InvalidHeader);
}
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
```

Apply the same pattern to `parent_c` in `dao_field_with_current_epoch` and `secondary_block_reward`.

### Proof of Concept

1. Construct or locate a block header whose DAO field has `ar == 0` (bytes 8–15 of the DAO field are all zero).
2. Store a DAO cell whose deposit block is that header.
3. Call the `calculate_dao_maximum_withdraw` RPC with the corresponding `out_point` and any valid withdrawing block hash with a higher block number.
4. The node panics at:

```
thread 'tokio-runtime-worker' panicked at 'attempt to divide by zero'
util/dao/src/lib.rs:154
```

The relevant division without guard: [1](#0-0) 

The same unguarded pattern in `dao_field_with_current_epoch`: [2](#0-1) [3](#0-2) 

The same unguarded pattern in `secondary_block_reward`: [4](#0-3) 

The RPC entry point that exposes this to unprivileged callers: [5](#0-4) 

The genesis-level guard that exists for `c == 0` but has no analog for `ar == 0` at runtime: [6](#0-5)

### Citations

**File:** util/dao/src/lib.rs (L146-154)
```rust
        let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
        let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
```

**File:** util/dao/src/lib.rs (L201-203)
```rust
        let (_, target_parent_c, _, target_parent_u) = extract_dao_data(target_parent.dao());
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
```

**File:** util/dao/src/lib.rs (L242-243)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
```

**File:** util/dao/src/lib.rs (L256-257)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
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

**File:** util/dao/utils/src/lib.rs (L88-92)
```rust
    // C cannot be zero, otherwise DAO stats calculation might result in
    // division by zero errors.
    if c == Capacity::zero() {
        return Err(DaoError::ZeroC);
    }
```

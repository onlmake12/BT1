### Title
Missing Zero-Check on `deposit_ar` Before Division in DAO Withdrawal Calculation — (`File: util/dao/src/lib.rs`)

### Summary
`calculate_maximum_withdraw` in `util/dao/src/lib.rs` reads `deposit_ar` from the deposit block header's DAO field and immediately uses it as a divisor without checking whether it is zero. In Rust, integer division by zero causes an unconditional panic, crashing the node process. The analogous protection (`c != 0`) is explicitly applied at genesis in `genesis_dao_data_with_satoshi_gift`, but no equivalent guard exists for `deposit_ar` at the point of use.

### Finding Description
In `calculate_maximum_withdraw` (lines 146–154 of `util/dao/src/lib.rs`):

```rust
let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
let output_capacity: Capacity = output.capacity().into();
let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);   // ← no zero-check; panics if deposit_ar == 0
```

`deposit_ar` is a raw `u64` decoded from the 32-byte DAO field of the deposit block header via `extract_dao_data`. No guard is applied before the division. If `deposit_ar` is zero the Rust runtime raises `attempt to divide by zero` and the node process terminates.

The same unchecked division pattern appears in `dao_field_with_current_epoch` (lines 242–243 and 256–257) and `secondary_block_reward` (line 202–203), all dividing by `parent_c` / `target_parent_c` without a zero check.

By contrast, `genesis_dao_data_with_satoshi_gift` explicitly guards against this class of error:

```rust
// C cannot be zero, otherwise DAO stats calculation might result in
// division by zero errors.
if c == Capacity::zero() {
    return Err(DaoError::ZeroC);
}
```

No equivalent guard exists for `deposit_ar` at the call site in `calculate_maximum_withdraw`.

### Impact Explanation
A Rust integer division by zero is an unconditional panic that terminates the process. Any code path that reaches `calculate_maximum_withdraw` with a zero `deposit_ar` crashes the CKB node. The public RPC endpoint `calculate_dao_maximum_withdraw` calls this function directly with caller-supplied `out_point` and header-hash arguments, making it reachable by any unprivileged RPC caller. If a block whose DAO field encodes `ar = 0` is present in the node's store (see Likelihood), a single malformed RPC call crashes the node.

### Likelihood Explanation
On a fully-validated mainnet/testnet chain the genesis block always sets `ar = DEFAULT_GENESIS_ACCUMULATE_RATE = 10_000_000_000_000_000`, and `DaoHeaderVerifier` rejects any subsequent block whose DAO field does not match the computed value. However:

1. `DaoHeaderVerifier` is gated behind `Switch::DISABLE_DAOHEADER` — nodes running with this flag (e.g., during fast-sync, testing, or custom tooling) accept blocks without DAO-field validation.
2. A custom or dev-chain genesis can be constructed with `ar = 0` in the DAO field; `genesis_dao_data_with_satoshi_gift` only checks `c != 0`, not `ar != 0`.
3. The `calculate_dao_maximum_withdraw` RPC does not validate that the referenced `out_point` belongs to a properly-validated chain segment; it fetches whatever header is in the store.

Under any of these conditions an unprivileged RPC caller can trigger the panic with a single request.

### Recommendation
Add an explicit zero-check for `deposit_ar` before the division in `calculate_maximum_withdraw`, mirroring the existing `ZeroC` guard:

```rust
if deposit_ar == 0 {
    return Err(DaoError::InvalidHeader);
}
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
```

Apply the same defensive check to `parent_c` in `dao_field_with_current_epoch` and `target_parent_c` in `secondary_block_reward`.

### Proof of Concept
1. Start a CKB dev node with `Switch::DISABLE_DAOHEADER` enabled, or craft a genesis whose DAO field has `ar = 0` (only `c != 0` is enforced by `DAOVerifier::verify`).
2. Mine one block so a transaction exists in the store.
3. Call the RPC:
   ```json
   {
     "method": "calculate_dao_maximum_withdraw",
     "params": [
       { "tx_hash": "<tx_in_block_with_ar_0>", "index": "0x0" },
       "<any_withdrawing_header_hash>"
     ]
   }
   ```
4. `calculate_maximum_withdraw` extracts `deposit_ar = 0` from the deposit header's DAO field and executes `/ u128::from(0)`, triggering a Rust panic and crashing the node process.

**Root cause lines:** [1](#0-0) 

**Analogous missing guard (genesis only):** [2](#0-1) 

**RPC entry point that exposes the path to unprivileged callers:** [3](#0-2) 

**`dao_field_with_current_epoch` unchecked divisions:** [4](#0-3) 

**`secondary_block_reward` unchecked division:** [5](#0-4)

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

**File:** util/dao/src/lib.rs (L201-204)
```rust
        let (_, target_parent_c, _, target_parent_u) = extract_dao_data(target_parent.dao());
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L242-257)
```rust
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
```

**File:** util/dao/utils/src/lib.rs (L88-92)
```rust
    // C cannot be zero, otherwise DAO stats calculation might result in
    // division by zero errors.
    if c == Capacity::zero() {
        return Err(DaoError::ZeroC);
    }
```

**File:** rpc/src/module/experiment.rs (L259-267)
```rust
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

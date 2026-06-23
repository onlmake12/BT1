Looking at the external report's vulnerability class — arithmetic overflow/underflow in a critical computation path that can cause a revert/panic, blocking contract interactions — I need to find an analog in CKB where a similar arithmetic failure can block critical operations.

I examined the most relevant CKB computation paths:

**DAO field calculation (`util/dao/src/lib.rs`)**

The `dao_field_with_current_epoch` function performs divisions by `parent_c.as_u64()`: [1](#0-0) [2](#0-1) 

And `secondary_block_reward` divides by `target_parent_c.as_u64()`: [3](#0-2) 

A zero denominator would panic. However, `c` is guarded at genesis: [4](#0-3) 

And `c` is monotonically increasing (`current_c = parent_c.safe_add(current_g)`), so it can never reach zero post-genesis.
<cite repo="Alyssadaypin/ckb--011" path="util/dao/src/lib.

### Citations

**File:** util/dao/src/lib.rs (L202-203)
```rust
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

**File:** util/dao/utils/src/lib.rs (L88-92)
```rust
    // C cannot be zero, otherwise DAO stats calculation might result in
    // division by zero errors.
    if c == Capacity::zero() {
        return Err(DaoError::ZeroC);
    }
```

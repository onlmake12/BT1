### Title
DAO Phase-2 Withdrawal Silently Discards Accrued Interest for Genesis-Block Deposits Due to Zero-Sentinel Collision — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` uses `deposited_block_number > 0` as the sole sentinel to distinguish a Phase-1 (deposit) DAO cell from a Phase-2 (withdrawing) DAO cell. Block 0 — the genesis block — is a valid deposit block. Any DAO cell deposited at block 0 stores `[0x00; 8]` in its Phase-2 cell data, which is byte-for-byte identical to a Phase-1 cell. The validator therefore silently treats the Phase-2 cell as a Phase-1 cell, returns face value instead of face value + interest, and the user's accrued DAO interest is permanently inaccessible.

---

### Finding Description

The NervosDAO two-phase withdrawal protocol encodes the deposit block number into the 8-byte cell data of the Phase-2 (withdrawing) cell. A Phase-1 (deposit) cell carries `[0x00; 8]`. A Phase-2 cell carries the little-endian u64 block number of the original deposit.

In `transaction_maximum_withdraw` (`util/dao/src/lib.rs`, lines 61–116):

```rust
let deposited_block_number =
    match self.data_loader.load_cell_data(cell_meta) {
        Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
        _ => 0,
    };
if deposited_block_number > 0 {          // ← sentinel check
    // … resolve deposit header, compute interest …
} else {
    Ok(output.capacity().into())         // ← returns face value only
}
```

The sentinel `> 0` is correct for every deposit block except block 0. When the original deposit was committed in block 0, the Phase-2 cell data is `[0x00; 8]`, so `deposited_block_number` decodes to `0`. The branch falls through to the `else` arm and returns the raw face-value capacity, skipping the entire interest calculation.

The same zero-sentinel pattern appears in the test verifier (`test/src/specs/dao/dao_verifier.rs`, line 255: `if deposited_number == 0 { return false; }`), confirming the assumption is load-bearing across the codebase.

---

### Impact Explanation

**1. Permanent loss of DAO interest for the depositor.**
`transaction_fee` (`util/dao/src/lib.rs`, lines 30–36) computes `maximum_withdraw − outputs_capacity`. When the user correctly claims interest (outputs > face value), `maximum_withdraw` is only the face value, so `safe_sub` underflows and returns `DaoError`. The withdrawal transaction is rejected by every CKB node; the user cannot reclaim their interest.

**2. DAO accumulator `s` inflated in the block DAO field.**
`withdrawed_interests` (`util/dao/src/lib.rs`, lines 312–333) calls `transaction_maximum_withdraw` for every transaction in a block. For a genesis-block deposit, it returns face value, so `maximum_withdraws − input_capacities = 0`. The actual interest is never subtracted from `parent_s` in `dao_field_with_current_epoch` (line 254: `current_s = parent_s + nervosdao_issuance − withdrawed_interests`). The DAO secondary-issuance pool `s` is permanently inflated by the unaccounted interest, skewing future depositor rewards.

---

### Likelihood Explanation

On the public mainnet the genesis block contains no user DAO deposits, so the collision cannot be triggered there. However:

- CKB's chain-spec system explicitly supports custom genesis blocks (devnet / testnet). A chain operator or script author who places a DAO-type output in the genesis block — a supported, documented configuration — immediately creates the conditions for this bug.
- The `genesis_dao_data_with_satoshi_gift` function (`util/dao/utils/src/lib.rs`) already processes genesis-block outputs with DAO type scripts, confirming the protocol considers genesis-block DAO cells valid.
- Any future hard-fork or chain migration that seeds DAO cells at block 0 would trigger the bug on mainnet.

Likelihood is **low on current mainnet, medium on devnet/testnet chains, and a latent mainnet risk** for any future genesis-seeded DAO cell.

---

### Recommendation

Replace the `> 0` sentinel with an explicit encoding that cannot collide with a valid block number. Two options:

1. **Use `u64::MAX` as the Phase-1 sentinel.** Store `u64::MAX` in the Phase-1 cell data and check `deposited_block_number != u64::MAX` in `transaction_maximum_withdraw`. The DAO C script must be updated in lockstep.

2. **Add a separate flag byte.** Extend the cell data to 9 bytes: byte 0 is a phase flag (`0x00` = deposit, `0x01` = withdrawing), bytes 1–8 are the block number. The Rust validator and the C script both read the flag.

Either fix eliminates the ambiguity between "this is a Phase-1 cell" and "this is a Phase-2 cell whose deposit happened at block 0."

---

### Proof of Concept

1. Construct a chain spec that includes a DAO-type output in the genesis (block 0) cellbase or genesis transaction.
2. Mine blocks to accumulate interest (`ar` grows over time).
3. Submit a Phase-1 prepare transaction spending the genesis DAO cell; the DAO C script writes `0x0000000000000000` (block 0 in LE) into the Phase-2 cell data.
4. After the lock period, submit the Phase-2 withdrawal transaction claiming face value + interest.
5. `DaoCalculator::transaction_maximum_withdraw` reads `deposited_block_number = 0`, enters the `else` branch, and returns face value only.
6. `transaction_fee` computes `face_value − (face_value + interest)`, which underflows; the node returns `DaoError` and rejects the block.
7. The user's interest is permanently inaccessible; no valid block can ever include this withdrawal.

**Root cause line:** [1](#0-0) 

**Silent face-value fallback:** [2](#0-1) 

**Fee underflow site:** [3](#0-2) 

**DAO-field `s` inflation site:** [4](#0-3) 

**Same sentinel in test verifier (confirming load-bearing assumption):** [5](#0-4)

### Citations

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
```

**File:** util/dao/src/lib.rs (L66-66)
```rust
                        if deposited_block_number > 0 {
```

**File:** util/dao/src/lib.rs (L114-116)
```rust
                        } else {
                            Ok(output.capacity().into())
                        }
```

**File:** util/dao/src/lib.rs (L252-254)
```rust
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
```

**File:** test/src/specs/dao/dao_verifier.rs (L254-257)
```rust
        let deposited_number = LittleEndian::read_u64(&input_data.raw_data()[0..8]);
        if deposited_number == 0 {
            return false;
        }
```

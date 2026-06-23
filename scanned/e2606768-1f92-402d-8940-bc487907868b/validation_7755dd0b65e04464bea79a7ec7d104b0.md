### Title
Rust `DaoCalculator` reads DAO witness header-dep index as `u64` while on-chain DAO script reads it as `u8`, causing valid DAO withdrawals to be permanently rejected from tx-pool — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` in `util/dao/src/lib.rs` extracts the deposit-block `header_deps` index from the witness `input_type` field by reading the full 8 bytes as a little-endian `u64`. The on-chain DAO C script, however, reads this same field as a single byte (`u8`). When a transaction encodes an index whose full `u64` value exceeds 255 but whose lowest byte is a valid small index (e.g., `257` → lowest byte `1`), the two interpretations diverge: the C VM resolves the correct deposit header at position `1`, while the Rust calculator resolves a different (or absent) header at position `257`. The Rust calculator then fails its block-number consistency check and returns `DaoError::InvalidOutPoint`, causing `check_tx_fee` to reject the transaction from the tx-pool before the C VM ever runs. The transaction is therefore permanently unsubmittable despite being on-chain valid.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` parses the witness index:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
``` [1](#0-0) 

It then uses that `u64` value directly to index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
    .and_then(|hash| header_deps.get(&hash))
    .ok_or(DaoError::InvalidOutPoint)
``` [2](#0-1) 

After resolving the header, the code checks that the resolved deposit header's block number matches the 8-byte block number stored in the cell data:

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
``` [3](#0-2) 

The on-chain DAO C script reads the same `input_type` bytes as a **single byte** (`u8`). For a witness value of `257` (little-endian u64: `[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`):

- **C VM** reads byte `0x01` → index `1` → resolves `header_deps[1]` (the correct deposit block, number 100) → **accepts**.
- **Rust** reads full u64 `257` → resolves `header_deps[257]` (the withdraw block, number 200) → block-number check `200 != 100` → **rejects**.

The discrepancy is explicitly documented in the test suite:

```
// Rust resolves index 257 → withdraw block (number 200), but cell data
// says deposited at block 100. Block number check catches the mismatch.
assert!(result.is_err(), "expected Err, got {result:?}");
``` [4](#0-3) 

The `check_tx_fee` function in the tx-pool calls `DaoCalculator::transaction_fee` (which calls `transaction_maximum_withdraw`) as a **pre-verification gate**:

```rust
let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
``` [5](#0-4) 

If `check_tx_fee` returns an error, the transaction is rejected before `verify_rtx` (which runs the C VM) is ever called. The transaction is therefore permanently blocked from the tx-pool.

---

### Impact Explanation

A DAO depositor who constructs a phase-2 withdrawal transaction with more than 256 `header_deps` entries and places the deposit block header at an index whose full `u64` value exceeds 255 (e

### Citations

**File:** util/dao/src/lib.rs (L91-92)
```rust
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
```

**File:** util/dao/src/lib.rs (L93-99)
```rust
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
                                        .and_then(|hash| header_deps.get(&hash))
                                        .ok_or(DaoError::InvalidOutPoint)
                                })?;
```

**File:** util/dao/src/lib.rs (L105-107)
```rust
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
```

**File:** util/dao/src/tests.rs (L534-536)
```rust
    // Rust resolves index 257 → withdraw block (number 200), but cell data
    // says deposited at block 100. Block number check catches the mismatch.
    assert!(result.is_err(), "expected Err, got {result:?}");
```

**File:** tx-pool/src/process.rs (L289-290)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```

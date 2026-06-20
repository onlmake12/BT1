### Title
Unbounded Blake2b Hashing Charged at Fixed 8-Cycle Rate in `CellField::LockHash` Syscall — (`script/src/syscalls/load_cell.rs`)

---

### Summary

`LOAD_CELL_BY_FIELD_SYSCALL_NUMBER` with `CellField::LockHash` recomputes a full Blake2b hash over the entire lock script serialization on every call, but charges only `transferred_byte_cycles(32) = 8 cycles` (the size of the 32-byte hash output). There is no caching of the lock hash in `CellMeta`. An attacker can craft a transaction referencing a cell dep with a large lock script and loop over `LockHash` queries, causing verification nodes to perform orders-of-magnitude more cryptographic work than the cycle limit implies.

---

### Finding Description

In `load_by_field`, the `CellField::LockHash` branch is:

```rust
CellField::LockHash => {
    let hash = output.calc_lock_hash();
    let bytes = hash.as_bytes();
    (SUCCESS, store_data(machine, &bytes)?)
}
``` [1](#0-0) 

`store_data` returns `len = 32` (the hash output size). The cycle charge is applied in `ecall`:

```rust
machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
``` [2](#0-1) 

With `BYTES_PER_CYCLE = 4`, this yields `ceil(32 / 4) = 8 cycles` regardless of lock script size. [3](#0-2) 

`calc_lock_hash` delegates to `calc_script_hash`, which calls `blake2b_256(self.as_slice())` — hashing the **entire lock script byte representation** including `code_hash` (32 bytes), `hash_type` (1 byte), and `args` (up to ~32 KB): [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) 

`CellMeta` caches `mem_cell_data_hash` for cell data, but has **no cached lock hash field**. Every syscall invocation recomputes the hash from scratch: [8](#0-7) 

The same uncached recomputation applies to `CellField::TypeHash` at line 149–155. [9](#0-8) 

---

### Impact Explanation

**Cycle amplification ratio** for a lock script of size N bytes:

| Lock script size | Actual hashing work (in cycle-equivalent) | Cycles charged | Amplification |
|---|---|---|---|
| 32 bytes (minimal) | 8 cycles | 8 cycles | 1× |
| 4 KB | 1,024 cycles | 8 cycles | 128× |
| 32 KB | 8,192 cycles | 8 cycles | **1,024×** |

With `MAX_BLOCK_CYCLES = 3,500,000,000`: [10](#0-9) 

- Each loop iteration costs ~28 cycles (20 RISC-V loop instructions + 8 syscall cycles)
- Maximum iterations within cycle limit: ~125 million
- Each iteration hashes 32 KB at ~1 GB/s Blake2b throughput ≈ 32 µs real time
- **Total real verification time: ~125M × 32 µs ≈ 4,000 seconds (~67 minutes)**

A transaction that "costs" the maximum allowed block cycles would take ~67 minutes to verify instead of ~3.5 seconds. Every node that receives and verifies this transaction (via P2P relay or tx-pool admission) is affected.

---

### Likelihood Explanation

The attack requires:
1. Committing a cell with a large lock script (e.g., 32 KB args) in a prior transaction — feasible within the block byte limit of 597 KB.
2. Submitting a transaction that references it as a cell dep and loops `LockHash` queries — standard unprivileged transaction submission.

No special role, leaked key, or majority hashpower is required. The transaction is valid and will be relayed by peers. [11](#0-10) 

---

### Recommendation

1. **Cache the lock hash in `CellMeta`**: Add an `Option<Byte32>` field (analogous to `mem_cell_data_hash`) that is populated on first computation and reused on subsequent calls.
2. **Charge cycles proportional to input size**: For `CellField::LockHash` and `CellField::TypeHash`, charge `transferred_byte_cycles(lock_script.as_slice().len())` instead of (or in addition to) `transferred_byte_cycles(32)`.
3. Apply the same fix to `CellField::TypeHash`.

---

### Proof of Concept

1. Craft a transaction `T1` with an output whose lock script has ~32 KB of `args`. Submit and confirm on-chain.
2. Craft transaction `T2` with `T1`'s output as a cell dep. The lock script is a tight loop:
   ```c
   while (1) {
       uint64_t len = 32;
       uint8_t hash[32];
       ckb_load_cell_by_field(hash, &len, 0, 0, CKB_SOURCE_CELL_DEP, CKB_CELL_FIELD_LOCK_HASH);
   }
   ```
3. Submit `T2` to the tx pool. Measure wall-clock verification time vs. `MAX_BLOCK_CYCLES / 1e9` seconds.
4. Assert: actual time >> expected time, confirming the amplification.

The cycle counter will exhaust at `MAX_BLOCK_CYCLES`, but the wall-clock time will be ~1,000× longer than a transaction spending the same cycles on simple arithmetic instructions.

### Citations

**File:** script/src/syscalls/load_cell.rs (L137-141)
```rust
            CellField::LockHash => {
                let hash = output.calc_lock_hash();
                let bytes = hash.as_bytes();
                (SUCCESS, store_data(machine, &bytes)?)
            }
```

**File:** script/src/syscalls/load_cell.rs (L149-156)
```rust
            CellField::TypeHash => match output.type_().to_opt() {
                Some(type_) => {
                    let hash = type_.calc_script_hash();
                    let bytes = hash.as_bytes();
                    (SUCCESS, store_data(machine, &bytes)?)
                }
                None => (ITEM_MISSING, 0),
            },
```

**File:** script/src/syscalls/load_cell.rs (L191-191)
```rust
        machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```

**File:** script/src/cost_model.rs (L7-12)
```rust
pub const BYTES_PER_CYCLE: u64 = 4;

/// Calculates how many cycles spent to load the specified number of bytes.
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
```

**File:** util/gen-types/src/extension/calc_hash.rs (L19-21)
```rust
    fn calc_hash(&self) -> packed::Byte32 {
        blake2b_256(self.as_slice()).into()
    }
```

**File:** util/gen-types/src/extension/calc_hash.rs (L87-89)
```rust
    pub fn calc_script_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
```

**File:** util/gen-types/src/extension/calc_hash.rs (L98-100)
```rust
    pub fn calc_lock_hash(&self) -> packed::Byte32 {
        self.lock().calc_script_hash()
    }
```

**File:** util/hash/src/lib.rs (L86-92)
```rust
fn inner_blake2b_256<T: AsRef<[u8]>>(s: T) -> [u8; 32] {
    let mut result = [0u8; 32];
    let mut blake2b = new_blake2b();
    blake2b.update(s.as_ref());
    blake2b.finalize(&mut result);
    result
}
```

**File:** util/types/src/core/cell.rs (L37-54)
```rust
pub struct CellMeta {
    /// The cell output data structure.
    pub cell_output: CellOutput,
    /// The outpoint referencing this cell.
    pub out_point: OutPoint,
    /// Transaction information where this cell was created.
    pub transaction_info: Option<TransactionInfo>,
    /// Size of the cell data in bytes.
    pub data_bytes: u64,
    /// In memory cell data
    /// A live cell either exists in memory or DB
    /// must check DB if this field is None
    pub mem_cell_data: Option<Bytes>,
    /// memory cell data hash
    /// A live cell either exists in memory or DB
    /// must check DB if this field is None
    pub mem_cell_data_hash: Option<Byte32>,
}
```

**File:** spec/src/consensus.rs (L83-84)
```rust
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

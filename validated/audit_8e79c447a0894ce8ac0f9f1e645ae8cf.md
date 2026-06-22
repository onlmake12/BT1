### Title
Silent Offset Clamping in `store_data` Syscall Utility Returns `SUCCESS` Instead of `SLICE_OUT_OF_BOUND` for Out-of-Range Offsets - (File: `script/src/syscalls/utils.rs`)

---

### Summary

The shared `store_data` utility function used by all major CKB load syscalls silently clamps an attacker-influenced, script-supplied offset to the data length via `cmp::min` instead of returning `SLICE_OUT_OF_BOUND`. When a script passes an offset that exceeds the actual data length, the syscall returns `SUCCESS` with `full_size = 0` written to the size address. This is inconsistent with the explicitly correct bounds-checking behavior in `exec.rs` and can cause scripts that dynamically compute offsets from attacker-controlled transaction data to make incorrect authorization decisions.

---

### Finding Description

The `store_data` function in `script/src/syscalls/utils.rs` is the central utility called by every major CKB load syscall to copy data into VM memory. It reads the offset from register `A2` and applies a silent clamp:

```rust
// script/src/syscalls/utils.rs, line 16
let offset = cmp::min(data_len, machine.registers()[A2].to_u64());
```

When `A2 >= data_len`, `offset` is clamped to `data_len`, making `full_size = data_len - offset = 0` and `real_size = 0`. The function then writes `full_size = 0` to the size address and returns `Ok(0)`. The calling syscall handler then sets `A0 = SUCCESS`. [1](#0-0) 

This affects every syscall that delegates to `store_data`:

- `LoadCell` (`load_cell.rs`) — `load_full` and `load_by_field` both call `store_data`
- `LoadWitness` (`load_witness.rs`) — calls `store_data` directly
- `LoadTx` (`load_tx.rs`) — calls `store_data` for both tx hash and full tx
- `LoadHeader` (`load_header.rs`) — calls `store_data` in `load_full`
- `LoadInput` (`load_input.rs`) — calls `store_data` in `load_full` and `load_by_field`
- `LoadScript` (`load_script.rs`) — calls `store_data`
- `LoadScriptHash` (`load_script_hash.rs`) — calls `store_data`
- `LoadBlockExtension` (`load_block_extension.rs`) — calls `store_data` [2](#0-1) [3](#0-2) [4](#0-3) 

By contrast, the `exec` syscall explicitly checks the offset against the data size and returns `SLICE_OUT_OF_BOUND`:

```rust
// script/src/syscalls/exec.rs, lines 139-141
if offset >= data_size {
    machine.set_register(A0, Mac::REG::from_u8(SLICE_OUT_OF_BOUND));
    return Ok(true);
};
``` [5](#0-4) 

The same explicit check exists in `spawn.rs`: [6](#0-5) 

And in `load_cell_data.rs` for the `load_data_as_code` path: [7](#0-6) 

This inconsistency confirms the silent clamp in `store_data` is unintended behavior, not a design choice.

---

### Impact Explanation

A script that dynamically computes an offset from attacker-controlled transaction data (e.g., a field parsed from a witness or cell data) and passes it to any of the affected load syscalls will receive `SUCCESS` with `full_size = 0` instead of `SLICE_OUT_OF_BOUND`. The script cannot distinguish between "the data is genuinely empty" and "my offset was out of range." A script that branches on the error code to detect out-of-range access will silently proceed down the "success/empty" path, potentially bypassing authorization checks or making incorrect state decisions. This is a script-level logic corruption triggered by attacker-crafted transaction data.

---

### Likelihood Explanation

Any script that:
1. Reads a length or offset value from a witness, cell data, or other transaction field (all attacker-supplied), and
2. Uses that value as the offset argument (register `A2`) to a load syscall,

is vulnerable. This is a common pattern in scripts that implement partial-data loading or streaming reads. The attacker only needs to submit a transaction with a crafted witness or cell data value that causes the computed offset to exceed the data length. No special privileges are required — any transaction sender can trigger this.

---

### Recommendation

Replace the silent `cmp::min` clamp in `store_data` with an explicit bounds check that returns `SLICE_OUT_OF_BOUND` when the offset exceeds the data length, consistent with the behavior already implemented in `exec.rs`, `spawn.rs`, and `load_data_as_code`:

```rust
pub fn store_data<Mac: SupportMachine>(machine: &mut Mac, data: &[u8]) -> Result<u8, VMError> {
    let addr = machine.registers()[A0].to_u64();
    let size_addr = machine.registers()[A1].clone();
    let data_len = data.len() as u64;
    let offset = machine.registers()[A2].to_u64();

    if offset > data_len {
        return Ok(SLICE_OUT_OF_BOUND);
    }
    // ... rest of the function
}
```

The return type and all callers would need to be updated to propagate the error code rather than always returning `SUCCESS`.

---

### Proof of Concept

1. Deploy a script that calls `ckb_load_witness` with `A2` (offset) set to a value read from the first byte of the witness (attacker-controlled).
2. Submit a transaction where the witness byte encodes a value larger than the witness length.
3. The script calls `ckb_load_witness` with an out-of-range offset.
4. `store_data` clamps the offset to `data_len`, writes `full_size = 0` to the size address, and returns `SUCCESS`.
5. The script observes `A0 = SUCCESS` and `*size_addr = 0`, interprets this as "witness is empty," and proceeds with incorrect logic — for example, skipping a signature check that was gated on a non-empty witness. [8](#0-7) [9](#0-8)

### Citations

**File:** script/src/syscalls/utils.rs (L12-27)
```rust
pub fn store_data<Mac: SupportMachine>(machine: &mut Mac, data: &[u8]) -> Result<u64, VMError> {
    let addr = machine.registers()[A0].to_u64();
    let size_addr = machine.registers()[A1].clone();
    let data_len = data.len() as u64;
    let offset = cmp::min(data_len, machine.registers()[A2].to_u64());

    let size = machine.memory_mut().load64(&size_addr)?.to_u64();
    let full_size = data_len - offset;
    let real_size = cmp::min(size, full_size);
    machine
        .memory_mut()
        .store64(&size_addr, &Mac::REG::from_u64(full_size))?;
    machine
        .memory_mut()
        .store_bytes(addr, &data[offset as usize..(offset + real_size) as usize])?;
    Ok(real_size)
```

**File:** script/src/syscalls/load_cell.rs (L86-94)
```rust
    fn load_full<Mac: SupportMachine>(
        &self,
        machine: &mut Mac,
        output: &CellOutput,
    ) -> Result<(u8, u64), VMError> {
        let data = output.as_slice();
        let wrote_size = store_data(machine, data)?;
        Ok((SUCCESS, wrote_size))
    }
```

**File:** script/src/syscalls/load_witness.rs (L64-75)
```rust
        let witness = self.fetch_witness(source, index as usize);
        if witness.is_none() {
            machine.set_register(A0, Mac::REG::from_u8(INDEX_OUT_OF_BOUND));
            return Ok(true);
        }
        let witness = witness.unwrap();
        let data = witness.raw_data();
        let wrote_size = store_data(machine, &data)?;

        machine.add_cycles_no_checking(transferred_byte_cycles(wrote_size))?;
        machine.set_register(A0, Mac::REG::from_u8(SUCCESS));
        Ok(true)
```

**File:** script/src/syscalls/load_tx.rs (L34-46)
```rust
        let wrote_size = match machine.registers()[A7].to_u64() {
            LOAD_TX_HASH_SYSCALL_NUMBER => {
                store_data(machine, self.rtx.transaction.hash().as_slice())?
            }
            LOAD_TRANSACTION_SYSCALL_NUMBER => {
                store_data(machine, self.rtx.transaction.data().as_slice())?
            }
            _ => return Ok(false),
        };

        machine.add_cycles_no_checking(transferred_byte_cycles(wrote_size))?;
        machine.set_register(A0, Mac::REG::from_u8(SUCCESS));
        Ok(true)
```

**File:** script/src/syscalls/exec.rs (L138-152)
```rust
        let data_size = data.len();
        if offset >= data_size {
            machine.set_register(A0, Mac::REG::from_u8(SLICE_OUT_OF_BOUND));
            return Ok(true);
        };
        let data = if length == 0 {
            data.slice(offset..data_size)
        } else {
            // Both offset and length are <= u32::MAX, so offset.checked_add(length) will be always a Some.
            let end = offset.checked_add(length).ok_or(VMError::MemOutOfBound)?;
            if end > data_size {
                machine.set_register(A0, Mac::REG::from_u8(SLICE_OUT_OF_BOUND));
                return Ok(true);
            }
            data.slice(offset..end)
```

**File:** script/src/syscalls/spawn.rs (L122-132)
```rust
        if offset >= full_length {
            machine.set_register(A0, Mac::REG::from_u8(SLICE_OUT_OF_BOUND));
            return Ok(true);
        }
        if length > 0 {
            let end = offset.checked_add(length).ok_or(VMError::MemOutOfBound)?;
            if end > full_length {
                machine.set_register(A0, Mac::REG::from_u8(SLICE_OUT_OF_BOUND));
                return Ok(true);
            }
        }
```

**File:** script/src/syscalls/load_cell_data.rs (L145-154)
```rust
        let content_end = content_offset
            .checked_add(content_size)
            .ok_or(VMError::MemOutOfBound)?;
        if content_offset >= cell.len() as u64
            || content_end > cell.len() as u64
            || content_size > memory_size
        {
            machine.set_register(A0, Mac::REG::from_u8(SLICE_OUT_OF_BOUND));
            return Ok(());
        }
```

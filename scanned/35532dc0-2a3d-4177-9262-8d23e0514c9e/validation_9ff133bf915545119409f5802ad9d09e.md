The code is fully readable. Let me trace the exact execution path.

**`store_data` in `script/src/syscalls/utils.rs` (lines 12–27):**

```
size  = load64(size_addr)   → 0   (attacker-controlled)
full_size = data_len - offset       → e.g. 208 (real header bytes)
real_size = min(size, full_size)    → min(0, 208) = 0
store64(size_addr, full_size)       → writes 208 back (size probe)
store_bytes(addr, data[0..0])       → writes nothing
return real_size                    → 0
```

**`load_full` in `load_header.rs` (lines 112–120):**
- calls `store_data` → gets `wrote_size = 0`
- returns `(SUCCESS, 0)`

**`ecall` in `load_header.rs` (lines 175):**
```rust
machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
// transferred_byte_cycles(0) = 0.div_ceil(4) = 0
// → charges 0 cycles
```

**`transferred_byte_cycles` in `script/src/cost_model.rs` (line 12):**
```rust
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    bytes.div_ceil(BYTES_PER_CYCLE)  // 0 → 0
}
```

The full DB lookup (`fetch_header` → `data_loader.get_header`) executes unconditionally before `store_data` is ever called. The cycle charge is computed only from `real_size` (bytes written), not from `full_size` (bytes fetched). When `size_addr` holds 0, the DB read is free.

---

### Title
Zero-cycle DB reads via `size=0` probe in `load_header` syscall — (`script/src/syscalls/load_header.rs`)

### Summary
`LoadHeader::ecall` performs a full header DB lookup and then charges cycles only for the bytes actually written to the script's buffer. When the script sets the size field to 0 (a valid "size probe" pattern), `store_data` returns `real_size=0`, `transferred_byte_cycles(0)=0`, and `add_cycles_no_checking(0)` charges nothing. The DB read is not metered.

### Finding Description

`ecall` unconditionally calls `fetch_header` (which calls `data_loader.get_header`, a live DB read) before inspecting the requested size. [1](#0-0) 

`load_full` delegates to `store_data`, which computes `real_size = min(size, full_size)`. [2](#0-1) 

When `size = 0`, `real_size = 0` and `store_data` returns 0. [3](#0-2) 

The cycle charge is `transferred_byte_cycles(0) = 0`. [4](#0-3) 

The same pattern exists in `load_block_extension`, `load_cell`, `load_input`, `load_witness`, `load_tx`, and `load_script` — all charge cycles based on `wrote_size` from `store_data`. [5](#0-4) 

### Impact Explanation

A script can call `LOAD_HEADER_SYSCALL_NUMBER` (2072) in a tight loop with `size_addr` pointing to a zero word. Each iteration:
- triggers one `get_header` DB read (RocksDB I/O)
- charges 0 data-transfer cycles

The only cycles consumed are the RISC-V instruction cycles for the loop body and the `ecall` instruction itself (O(1) per iteration). Within the default 70,000,000-cycle budget, a script can issue hundreds of thousands of DB reads that would normally be bounded by the proportional cycle cost (~52 cycles per 208-byte header). This breaks the invariant that the cycle budget bounds total node work, enabling low-cost I/O amplification during script verification.

### Likelihood Explanation

The attack requires only a valid transaction with at least one `header_dep`. No privileged access, no key material, no majority hashpower. The transaction can be submitted via the standard RPC (`send_transaction`), triggering script verification in the tx-pool on every receiving node. The pattern is trivially expressible in a RISC-V assembly loop.

### Recommendation

Charge cycles based on `full_size` (the actual data fetched from the DB), not `real_size` (the bytes written). In `ecall`, replace:

```rust
machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```

with a version that uses the full serialized size of the header, regardless of how many bytes the script requested. Alternatively, `store_data` should return both `real_size` and `full_size`, and the caller should charge on `full_size`.

### Proof of Concept

```c
// RISC-V pseudocode
// Precondition: tx has one header_dep at index 0
// A1 points to a word initialized to 0 (size_addr)
// A3 = 0 (index), A4 = HeaderDep source, A7 = 2072

loop:
    li a7, 2072          // LOAD_HEADER_SYSCALL_NUMBER
    li a3, 0             // index 0
    li a4, 0x4           // Source::HeaderDep
    li a0, buf           // output buffer (ignored)
    la a1, size_word     // *size_word = 0
    li a2, 0             // offset = 0
    ecall                // DB read, 0 cycles charged
    sw zero, 0(a1)       // reset size_word to 0 for next iteration
    addi t0, t0, 1
    blt t0, 10000, loop
```

Each of the 10,000 iterations performs one `get_header` DB read and charges 0 data-transfer cycles. Measuring RocksDB read counters vs. cycles charged will show 10,000 reads at ~0 metered cost. [6](#0-5) [7](#0-6) [4](#0-3)

### Citations

**File:** script/src/syscalls/load_header.rs (L112-120)
```rust
    fn load_full<Mac: SupportMachine>(
        &self,
        machine: &mut Mac,
        header: &HeaderView,
    ) -> Result<(u8, u64), VMError> {
        let data = header.data().as_bytes();
        let wrote_size = store_data(machine, &data)?;
        Ok((SUCCESS, wrote_size))
    }
```

**File:** script/src/syscalls/load_header.rs (L153-178)
```rust
    fn ecall(&mut self, machine: &mut Mac) -> Result<bool, VMError> {
        let load_by_field = match machine.registers()[A7].to_u64() {
            LOAD_HEADER_SYSCALL_NUMBER => false,
            LOAD_HEADER_BY_FIELD_SYSCALL_NUMBER => true,
            _ => return Ok(false),
        };

        let index = machine.registers()[A3].to_u64();
        let source = Source::parse_from_u64(machine.registers()[A4].to_u64())?;

        let header = self.fetch_header(source, index as usize);
        if let Err(err) = header {
            machine.set_register(A0, Mac::REG::from_u8(err));
            return Ok(true);
        }
        let header = header.unwrap();
        let (return_code, len) = if load_by_field {
            self.load_by_field(machine, &header)?
        } else {
            self.load_full(machine, &header)?
        };

        machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
        machine.set_register(A0, Mac::REG::from_u8(return_code));
        Ok(true)
    }
```

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

**File:** script/src/cost_model.rs (L10-12)
```rust
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
```

**File:** script/src/syscalls/load_block_extension.rs (L125-128)
```rust
        let wrote_size = store_data(machine, &data)?;

        machine.add_cycles_no_checking(transferred_byte_cycles(wrote_size))?;
        machine.set_register(A0, Mac::REG::from_u8(SUCCESS));
```

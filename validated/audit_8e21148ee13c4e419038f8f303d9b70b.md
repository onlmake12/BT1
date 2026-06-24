Audit Report

## Title
Zero-cycle DB reads via `size=0` probe in `load_header` syscall — (`script/src/syscalls/load_header.rs`)

## Summary
`LoadHeader::ecall` performs an unconditional header DB lookup via `fetch_header` before inspecting the requested buffer size. Cycle charges are computed from `store_data`'s return value (`real_size` = bytes written), not from the actual bytes fetched. When a script sets the size field to 0, `real_size=0`, `transferred_byte_cycles(0)=0`, and the DB read is entirely unmetered. The same pattern exists in `load_block_extension` and other load syscalls.

## Finding Description
In `ecall` (`load_header.rs` L163), `fetch_header` is called unconditionally, which calls `data_loader.get_header` — a live DB read. [1](#0-0) 

`load_full` delegates to `store_data`, which computes `real_size = min(size, full_size)`. [2](#0-1)  When `size=0` (attacker-controlled via `size_addr`), `real_size=0` and `store_data` returns 0. [3](#0-2) 

Back in `ecall`, the cycle charge is `transferred_byte_cycles(len)` where `len` is the value returned by `load_full` (i.e., `real_size=0`). [4](#0-3) 

`transferred_byte_cycles(0)` returns `0.div_ceil(4) = 0`. [5](#0-4) 

The same pattern is present in `load_block_extension.rs` at L125-127, where `wrote_size` from `store_data` is passed directly to `transferred_byte_cycles`. [6](#0-5) 

No guard exists between the DB fetch and the size inspection. The cycle accounting invariant — that the cycle budget bounds total node work — is broken.

## Impact Explanation
A script can call `LOAD_HEADER_SYSCALL_NUMBER` (2072) in a tight loop with `size_addr` pointing to a zero word. Each iteration triggers one `get_header` RocksDB I/O operation and charges 0 data-transfer cycles. Within the default 70,000,000-cycle budget, a script can issue hundreds of thousands of DB reads that would normally be bounded by ~52 cycles per 208-byte header. This constitutes low-cost I/O amplification during script verification, enabling a single cheap transaction to impose disproportionate I/O load on every verifying node. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The attack requires only a valid transaction with at least one `header_dep`. No privileged access, key material, or majority hashpower is needed. The transaction is submittable via the standard `send_transaction` RPC, triggering script verification in the tx-pool on every receiving node. The pattern is trivially expressible in a RISC-V assembly loop and requires no special tooling.

## Recommendation
Charge cycles based on `full_size` (bytes fetched from DB), not `real_size` (bytes written to the script buffer). In `store_data`, return both values, and have `ecall` charge on `full_size`. Alternatively, compute the serialized size of the header before calling `store_data` and use that for cycle accounting regardless of how many bytes the script requested. The same fix must be applied to all syscalls sharing this pattern: `load_block_extension`, `load_cell`, `load_input`, `load_witness`, `load_tx`, and `load_script`.

## Proof of Concept
```c
// RISC-V pseudocode
// Precondition: tx has one header_dep at index 0
// size_word is a memory location initialized to 0
loop:
    li a7, 2072          // LOAD_HEADER_SYSCALL_NUMBER
    li a3, 0             // index 0
    li a4, 0x4           // Source::HeaderDep
    la a0, buf           // output buffer (ignored)
    la a1, size_word     // *size_word = 0
    li a2, 0             // offset = 0
    ecall                // DB read, 0 data-transfer cycles charged
    sw zero, 0(a1)       // reset size_word to 0 for next iteration
    addi t0, t0, 1
    blt t0, 10000, loop
```
Each of the 10,000 iterations performs one `get_header` DB read and charges 0 data-transfer cycles. Verification: instrument RocksDB read counters and compare against metered cycles — 10,000 reads will appear at ~0 metered data-transfer cost. The same loop works against `LOAD_BLOCK_EXTENSION` syscall number with identical mechanics. [7](#0-6)

### Citations

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

**File:** script/src/syscalls/utils.rs (L18-27)
```rust
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

**File:** script/src/syscalls/load_block_extension.rs (L125-127)
```rust
        let wrote_size = store_data(machine, &data)?;

        machine.add_cycles_no_checking(transferred_byte_cycles(wrote_size))?;
```

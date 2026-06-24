All four files confirm the claims. Let me verify each step of the exploit chain against the actual code:

Audit Report

## Title
Zero-cycle DB reads via `size=0` probe in `load_header` and `load_block_extension` syscalls — (`script/src/syscalls/load_header.rs`, `script/src/syscalls/load_block_extension.rs`)

## Summary
`LoadHeader::ecall` performs an unconditional DB lookup via `fetch_header` before inspecting the script-controlled buffer size. Cycle charges are computed from `store_data`'s return value (`real_size` = bytes written to the script buffer), not from the bytes actually fetched from the DB. When a script sets the size field to 0, `real_size=0`, `transferred_byte_cycles(0)=0`, and the DB read is entirely unmetered. The identical pattern exists in `load_block_extension`. A script can loop this syscall within the normal cycle budget to trigger far more RocksDB reads than the cycle accounting was designed to permit, imposing disproportionate I/O load on every verifying node.

## Finding Description
In `ecall` (`load_header.rs` L163), `fetch_header` is called unconditionally. For `Source::Transaction(SourceEntry::HeaderDep)`, this resolves to `self.sg_data.tx_info.data_loader.get_header(&block_hash)` (`load_header.rs` L93) — a live DB read — before any size check occurs.

`load_full` delegates to `store_data` (`utils.rs` L12–28). `store_data` reads the script-controlled `size` from memory (`utils.rs` L18), computes `real_size = cmp::min(size, full_size)` (`utils.rs` L20), writes `full_size` back to `size_addr` (`utils.rs` L22–23), copies `real_size` bytes to the script buffer (`utils.rs` L25–26), and returns `real_size` (`utils.rs` L27). When `size=0`, `real_size=0` and the return value is 0.

Back in `ecall` (`load_header.rs` L175), the cycle charge is `transferred_byte_cycles(len)` where `len` is the value returned by `load_full` — i.e., `real_size=0`. `transferred_byte_cycles(0)` computes `0u64.div_ceil(4) = 0` (`cost_model.rs` L12). The DB read is therefore charged 0 data-transfer cycles.

The same pattern is confirmed in `load_block_extension.rs` L125–127: `wrote_size = store_data(machine, &data)?` followed immediately by `machine.add_cycles_no_checking(transferred_byte_cycles(wrote_size))?`. No guard exists between the DB fetch and the size inspection in either syscall. The cycle accounting invariant — that the cycle budget bounds total node work per transaction — is broken for any syscall following this pattern.

## Impact Explanation
This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** A script loop with `size=0` triggers one `get_header` RocksDB read per iteration while charging 0 data-transfer cycles. The loop body itself costs only the base RISC-V instruction cycles (~10–20 cycles). Within the 70,000,000-cycle budget, an attacker can issue on the order of 3–7 million DB reads per transaction verification, versus the ~1.1 million that would be possible if the 52-cycle data-transfer cost for a 208-byte header were correctly charged. Every node that verifies the transaction (tx-pool admission, block validation) performs this amplified I/O. A sustained stream of such transactions constitutes low-cost I/O amplification enabling network congestion.

## Likelihood Explanation
The attack requires only a valid transaction containing at least one `header_dep` and a script cell whose lock or type script executes the loop. No privileged access, key material, or majority hashpower is needed. The transaction is submittable via the standard `send_transaction` RPC. The RISC-V loop is trivially expressible and requires no special tooling. The attack is repeatable across any number of transactions and is not mitigated by any existing guard in the syscall path.

## Recommendation
Charge cycles based on `full_size` (bytes fetched from DB), not `real_size` (bytes written to the script buffer). Concretely: modify `store_data` to return `(real_size, full_size)`, and have each `ecall` pass `full_size` to `transferred_byte_cycles`. Alternatively, compute the serialized size of the fetched object before calling `store_data` and use that value for cycle accounting unconditionally. The same fix must be applied to every syscall sharing this pattern: `load_block_extension`, `load_cell`, `load_input`, `load_witness`, `load_tx`, and `load_script`.

## Proof of Concept
```c
// RISC-V pseudocode
// Precondition: tx has one header_dep at index 0; size_word initialized to 0
loop:
    li a7, 2072          // LOAD_HEADER_SYSCALL_NUMBER
    li a3, 0             // index 0
    li a4, 0x4           // Source::HeaderDep
    la a0, buf           // output buffer (ignored)
    la a1, size_word     // *size_word = 0
    li a2, 0             // offset = 0
    ecall                // triggers get_header DB read; 0 data-transfer cycles charged
    sw zero, 0(a1)       // reset size_word to 0 for next iteration
    addi t0, t0, 1
    blt t0, 100000, loop
```
Instrument RocksDB read counters during script verification and compare against metered data-transfer cycles: 100,000 iterations will show 100,000 `get_header` reads at ~0 metered data-transfer cost. The identical loop works against `LOAD_BLOCK_EXTENSION` syscall number. A unit test can assert that `transferred_byte_cycles` received by `add_cycles_no_checking` equals `full_size.div_ceil(4)` regardless of the `size` register value. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** script/src/syscalls/load_header.rs (L163-175)
```rust
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

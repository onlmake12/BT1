Audit Report

## Title
Zero-cycle DB reads via `size=0` probe in data-loading syscalls — (`script/src/syscalls/load_header.rs`)

## Summary
`LoadHeader::ecall` performs a full header DB lookup unconditionally before inspecting the requested buffer size. Cycle charging is computed from `store_data`'s return value (`real_size`), which is `min(size, full_size)`. When a script sets the size field to 0, `real_size=0`, `transferred_byte_cycles(0)=0`, and the DB read is entirely unmetered. The same pattern exists in every other data-loading syscall in the codebase.

## Finding Description
In `script/src/syscalls/load_header.rs` lines 163–175, `fetch_header` (which calls `data_loader.get_header`, a live RocksDB read) executes unconditionally before any size inspection. [1](#0-0) 

`load_full` delegates to `store_data`, which at `script/src/syscalls/utils.rs` lines 18–27 computes `real_size = min(size, full_size)` and returns `real_size`. [2](#0-1) 

When the script sets `*size_addr = 0`, `real_size = 0`, `store_data` returns 0, and `transferred_byte_cycles(0) = 0` at `script/src/cost_model.rs` line 12, so `add_cycles_no_checking(0)` charges nothing for the data transfer. [3](#0-2) 

The same pattern is confirmed in `load_block_extension.rs` lines 125–127, and the `grep_search` results show `transferred_byte_cycles` applied to `wrote_size` (not `full_size`) in `load_cell.rs`, `load_input.rs`, `load_tx.rs`, `load_witness.rs`, and `load_script.rs`. [4](#0-3) 

The root cause is that the cycle model charges for bytes *written* to the script buffer, not bytes *fetched* from the DB. These are decoupled by the size-probe pattern, which is a documented and valid usage of the syscall API.

## Impact Explanation
The 70,000,000-cycle budget is the primary mechanism bounding total node work per script. A normal `load_header` call on a 208-byte header costs ~52 data-transfer cycles plus ~8 instruction cycles ≈ 60 cycles/read, allowing ~1,166,667 reads per budget. With `size=0`, only the ~8 RISC-V instruction cycles are charged, allowing ~8,750,000 reads per budget — a ~7.5× amplification of RocksDB I/O relative to metered cost. Because the same flaw exists across all data-loading syscalls, an attacker can combine them for maximum I/O pressure. A transaction submitted via the standard `send_transaction` RPC triggers script verification on every receiving node in the tx-pool, making this a network-wide DoS vector. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**.

## Likelihood Explanation
The attack requires only a valid transaction with at least one `header_dep`. No privileged access, no key material, no majority hashpower is needed. The size-probe pattern (`*size_addr = 0`) is a standard two-phase load idiom in CKB scripts, so it is indistinguishable from legitimate usage at the API level. The transaction can be submitted by any unprivileged user via the public RPC, and every node that receives it will execute the amplified DB reads during tx-pool admission. The attack is trivially repeatable and scriptable.

## Recommendation
Charge cycles based on `full_size` (bytes fetched from the DB), not `real_size` (bytes written to the script buffer). In `ecall` for `load_header.rs` and all analogous syscalls, `store_data` should return both values, or a separate `load_full_size` helper should be introduced. The charge line:

```rust
machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```

should use the serialized size of the fetched object regardless of how many bytes the script requested. Alternatively, add a fixed per-syscall base cost that covers the DB lookup independently of the transfer size.

## Proof of Concept
Construct a transaction with one `header_dep`. In the lock/type script, execute the following RISC-V loop:

```asm
    la   a1, size_word      # size_word initialized to 0
    li   a7, 2072           # LOAD_HEADER_SYSCALL_NUMBER
    li   a3, 0              # index 0
    li   a4, 4              # Source::HeaderDep
    la   a0, buf
    li   a2, 0              # offset = 0
loop:
    ecall                   # DB read, 0 data-transfer cycles charged
    sw   zero, 0(a1)        # reset size_word to 0
    addi t0, t0, 1
    blt  t0, 10000, loop
```

Each of the 10,000 iterations triggers one `get_header` RocksDB read and charges 0 data-transfer cycles. Instrument RocksDB read counters before and after script verification to confirm 10,000 reads at ~0 metered data cost. Scale the loop count to the full 70M-cycle budget to demonstrate the full amplification ratio (~8.75M reads vs. the expected ~1.17M).

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

**File:** script/src/syscalls/load_block_extension.rs (L125-128)
```rust
        let wrote_size = store_data(machine, &data)?;

        machine.add_cycles_no_checking(transferred_byte_cycles(wrote_size))?;
        machine.set_register(A0, Mac::REG::from_u8(SUCCESS));
```

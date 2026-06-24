Audit Report

## Title
Zero-cycle DB reads via `size=0` probe in data-loading syscalls — (`script/src/syscalls/load_header.rs`)

## Summary
`LoadHeader::ecall` performs a full header DB lookup unconditionally before inspecting the requested buffer size. Cycle charging is computed from `store_data`'s return value (`real_size = min(size, full_size)`). When a script sets `size=0`, `real_size=0`, `transferred_byte_cycles(0)=0`, and the DB read is entirely unmetered. The same pattern exists in every other data-loading syscall in the codebase, enabling a ~7.5× amplification of RocksDB I/O relative to metered cycle cost.

## Finding Description
In `script/src/syscalls/load_header.rs` line 163, `self.fetch_header(source, index as usize)` executes unconditionally. For `Source::Transaction(SourceEntry::HeaderDep)`, this resolves to `self.sg_data.tx_info.data_loader.get_header(&block_hash)` at line 93 — a live data-loader read — before any inspection of the script-supplied buffer size. [1](#0-0) 

`load_full` delegates to `store_data` in `script/src/syscalls/utils.rs`. At line 20, `real_size = cmp::min(size, full_size)`. When the script passes `size=0`, `real_size=0`, and `store_data` returns `0`. [2](#0-1) 

The cycle charge at line 175 of `load_header.rs` is `transferred_byte_cycles(len)` where `len` is the return value of `store_data`. `transferred_byte_cycles(0)` in `script/src/cost_model.rs` line 12 evaluates to `0u64.div_ceil(4) = 0`. [3](#0-2) 

The identical pattern is confirmed in `load_block_extension.rs` lines 125–127 (`wrote_size` from `store_data` used for cycle charge), and `grep_search` confirms `transferred_byte_cycles` applied to the written-size return value in `load_cell.rs`, `load_input.rs`, `load_tx.rs`, `load_witness.rs`, and `load_script.rs`. [4](#0-3) 

No per-syscall base cost exists to cover the DB lookup independently of the transfer size. The only cycle charge for the data-loading path is `transferred_byte_cycles(wrote_size)`, which collapses to zero when `size=0`.

## Impact Explanation
The 70,000,000-cycle budget is the primary mechanism bounding total node work per script. A normal `load_header` call on a ~208-byte header costs ~52 data-transfer cycles plus ~8 RISC-V instruction cycles ≈ 60 cycles/read, allowing ~1,166,667 reads per budget. With `size=0`, only the ~8 instruction cycles are charged, allowing ~8,750,000 reads per budget — a ~7.5× amplification of RocksDB I/O relative to metered cost. A transaction submitted via the standard `send_transaction` RPC triggers script verification on every receiving node in the tx-pool, making this a network-wide DoS vector. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (10001–15000 points)**.

## Likelihood Explanation
The attack requires only a valid transaction with at least one `header_dep`. No privileged access, no key material, and no majority hashpower is needed. The size-probe pattern (`*size_addr = 0`) is a standard two-phase load idiom in CKB scripts, so it is indistinguishable from legitimate usage at the API level. The transaction can be submitted by any unprivileged user via the public RPC, and every node that receives it will execute the amplified DB reads during tx-pool admission. The attack is trivially repeatable and scriptable.

## Recommendation
Charge cycles based on `full_size` (bytes fetched from the DB), not `real_size` (bytes written to the script buffer). Modify `store_data` in `script/src/syscalls/utils.rs` to return both `real_size` and `full_size`, or introduce a separate helper that exposes `full_size`. Update the charge line in `load_header.rs` (and all analogous syscalls) from:

```rust
machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```

to use the serialized size of the fetched object regardless of how many bytes the script requested. Alternatively, add a fixed per-syscall base cost that covers the DB lookup independently of the transfer size, applied unconditionally before `store_data` is called.

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

Each of the 10,000 iterations triggers one `get_header` data-loader read and charges 0 data-transfer cycles. Instrument RocksDB read counters before and after script verification to confirm 10,000 reads at ~0 metered data cost. Scale the loop count to the full 70M-cycle budget to demonstrate the full amplification ratio (~8.75M reads vs. the expected ~1.17M).

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

### Title
Cycle Over-Charging via Inflated `memory_size` in `load_cell_data_as_code` Syscall - (File: `script/src/syscalls/load_cell_data.rs`)

### Summary

The `load_data_as_code` function in CKB's VM syscall layer charges cycles based on the caller-supplied `memory_size` parameter rather than the actual number of bytes loaded from the cell. A malicious script author can pass a `memory_size` far larger than `content_size`, causing the VM to charge more cycles than the actual work performed. Because cycles are the unit of resource accounting that determines whether a transaction is accepted and how it is priced relative to the block cycle limit, this is an analog to the Biconomy gas-refund inflation bug: an attacker-controlled input inflates a resource-accounting value beyond the real cost.

### Finding Description

In `load_data_as_code` (`script/src/syscalls/load_cell_data.rs`, lines 101–166), the syscall accepts six register arguments:

- `A0` = `addr` — destination VM memory address
- `A1` = `memory_size` — size of the memory region to map (caller-controlled)
- `A2` = `content_offset` — offset into the cell data
- `A3` = `content_size` — number of bytes to copy from the cell
- `A4` = `index`, `A5` = `source`

The validation at lines 148–154 only checks:
1. `content_offset < cell.len()`
2. `content_end <= cell.len()`
3. `content_size <= memory_size`

After passing validation, `init_pages` maps `memory_size` bytes (zero-padding the region beyond `content_size`), and then cycles are charged as:

```rust
machine.add_cycles_no_checking(transferred_byte_cycles(memory_size))?;
```

`transferred_byte_cycles(memory_size)` = `memory_size.div_ceil(4)`.

The actual bytes transferred from the cell are only `content_size` bytes (the slice `content_offset..content_end`). The remaining `memory_size - content_size` bytes are zero-padding added by `init_pages`. The cycle charge is therefore `transferred_byte_cycles(memory_size)` instead of `transferred_byte_cycles(content_size)`, meaning a script can inflate its cycle consumption by an arbitrary factor by setting `memory_size >> content_size`.

The constraint `content_size <= memory_size` (line 150) is the only relationship enforced between the two values. There is no upper bound on `memory_size` beyond the VM's address space limit (4 MiB for a standard CKB-VM instance). A script can set `content_size = 1` and `memory_size = 4 * 1024 * 1024`, causing `transferred_byte_cycles(4194304) = 1048576` cycles to be charged for loading a single byte.

Compare this to the `load_data` path (lines 35–99), which charges only `transferred_byte_cycles(wrote_size)` where `wrote_size` is the actual bytes written — the correct behavior.

### Impact Explanation

Cycles in CKB are the primary resource limit for script execution. The block cycle limit (`max_block_cycles`) caps how many cycles all transactions in a block may consume. The tx-pool uses `get_transaction_weight` (which takes `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`) to compute fee-rate and prioritize transactions. Inflating cycles via this syscall has two concrete effects:

1. **Cycle limit exhaustion / DoS of block space**: A script that calls `load_cell_data_as_code` with a large `memory_size` will consume a disproportionate share of the block's cycle budget, crowding out legitimate transactions. A single transaction can consume the entire `max_block_cycles` (default 70,000,000) by loading a 1-byte cell with `memory_size = 280,000,000` (70M × 4 bytes/cycle).

2. **Fee-rate manipulation**: Because `get_transaction_weight` uses actual verified cycles, a transaction that inflates its cycles will appear to have a higher weight, which can affect fee-rate comparisons and RBF logic in the tx-pool.

The attacker is a script author who deploys a lock or type script on-chain. Any transaction that references such a cell dep or input will execute the script, triggering the inflated cycle charge.

### Likelihood Explanation

The entry path is fully reachable by any unprivileged script author. Deploying a cell with a malicious script requires only a standard CKB transaction. Any node that processes a transaction referencing that script will execute it and incur the inflated cycle charge. The syscall number `2091` (`LOAD_CELL_DATA_AS_CODE_SYSCALL_NUMBER`) is a standard, documented syscall available to all CKB scripts. No special privileges, keys, or majority hashpower are required.

### Recommendation

Charge cycles based on `content_size` (the actual bytes loaded from the cell) rather than `memory_size` (the memory region size, which includes zero-padding):

```rust
// Replace line 163:
machine.add_cycles_no_checking(transferred_byte_cycles(memory_size))?;
// With:
machine.add_cycles_no_checking(transferred_byte_cycles(content_size))?;
```

If the intent is to charge for the full memory mapping operation (including zero-initialization of padding), the charge should be bounded or documented explicitly, and `memory_size` should be capped to a reasonable multiple of `content_size` or to the VM page size granularity.

### Proof of Concept

A CKB script (RISC-V) that inflates its cycle consumption:

```c
// Pseudocode for a CKB lock script
#include "ckb_syscalls.h"

int main() {
    // Load 1 byte of cell data but request 4MB memory mapping
    // This charges transferred_byte_cycles(4194304) = 1,048,576 cycles
    // instead of transferred_byte_cycles(1) = 1 cycle
    uint8_t buf[4096];
    uint64_t memory_size = 4 * 1024 * 1024; // 4 MiB
    uint64_t content_offset = 0;
    uint64_t content_size = 1;              // only 1 real byte
    // syscall: ckb_load_cell_data_as_code(buf, memory_size, content_offset, content_size, 0, CKB_SOURCE_CELL_DEP)
    // Cycles charged: ceil(4194304 / 4) = 1,048,576
    // Cycles actually needed: ceil(1 / 4) = 1
    return 0;
}
```

The root cause is at: [1](#0-0) 

where `memory_size` is used instead of `content_size` for cycle accounting, while the actual data loaded is only `content_size` bytes: [2](#0-1) 

The `transferred_byte_cycles` function that converts byte counts to cycles: [3](#0-2) 

The correct pattern used by all other syscalls (e.g., `load_witness`) charges only `wrote_size` — the actual bytes transferred: [4](#0-3)

### Citations

**File:** script/src/syscalls/load_cell_data.rs (L155-163)
```rust
        machine.memory_mut().init_pages(
            addr,
            memory_size,
            FLAG_EXECUTABLE | FLAG_FREEZED,
            Some(cell.slice((content_offset as usize)..(content_end as usize))),
            0,
        )?;
        sc.track_pages(machine, addr, memory_size, &data_piece_id, content_offset)?;
        machine.add_cycles_no_checking(transferred_byte_cycles(memory_size))?;
```

**File:** script/src/cost_model.rs (L10-12)
```rust
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
```

**File:** script/src/syscalls/load_witness.rs (L71-73)
```rust
        let wrote_size = store_data(machine, &data)?;

        machine.add_cycles_no_checking(transferred_byte_cycles(wrote_size))?;
```

Audit Report

## Title
`load_data_as_code` Charges Cycles on `memory_size` Instead of `content_size`, Enabling Low-Cost Verify Worker Saturation — (File: `script/src/syscalls/load_cell_data.rs`)

## Summary
In `load_data_as_code` (syscall 2091), cycles are charged using `transferred_byte_cycles(memory_size)` at line 163, where `memory_size` is a fully attacker-controlled register value. The only bounds guard (`content_size > memory_size`) does not cap `memory_size` itself. An attacker can submit transactions referencing a 1-byte cell dep with `memory_size = 64 MB`, consuming ~16.7 million cycles per syscall invocation and pinning all tx-pool verify workers at near-cycle-limit execution with negligible cost.

## Finding Description
In `load_data_as_code` (`script/src/syscalls/load_cell_data.rs`, lines 101–166), registers are read directly from the VM:

- `A1 → memory_size` (attacker-controlled, no upper bound)
- `A3 → content_size` (attacker-controlled, only constrained to `≤ memory_size`)

The bounds check at lines 148–154 only verifies that the data slice fits within the cell and that `content_size ≤ memory_size`. It does not cap `memory_size`:

```rust
if content_offset >= cell.len() as u64
    || content_end > cell.len() as u64
    || content_size > memory_size   // only guard
{
    machine.set_register(A0, Mac::REG::from_u8(SLICE_OUT_OF_BOUND));
    return Ok(());
}
```

After passing this check, `init_pages` is called with the full attacker-supplied `memory_size`, and cycles are charged on it:

```rust
machine.memory_mut().init_pages(
    addr, memory_size, FLAG_EXECUTABLE | FLAG_FREEZED,
    Some(cell.slice(content_offset..content_end)), 0,
)?;
sc.track_pages(machine, addr, memory_size, &data_piece_id, content_offset)?;
machine.add_cycles_no_checking(transferred_byte_cycles(memory_size))?; // ← memory_size
```

`transferred_byte_cycles` is defined in `script/src/cost_model.rs` as `bytes.div_ceil(4)`, so `transferred_byte_cycles(64 MB) = 67,108,864 / 4 = 16,777,216` cycles per call.

With `content_size = 1` (1-byte cell dep) and `memory_size = 64 MB`:
- Bounds check passes: `1 ≤ 64 MB` ✓
- `init_pages` initializes 64 MB of VM address space
- Cycles charged: 16,777,216 per call

Four such calls per transaction consume ~67.1 million cycles. The transaction is rejected after hitting `max_tx_verify_cycles`, but only after the full `init_pages` work has been performed by the verify worker.

The `content_size == 0` special case (lines 131–134) returns `Bytes::new()`, causing the bounds check at line 148 to always return `SLICE_OUT_OF_BOUND` (since `cell.len() == 0`), so the attack requires `content_size ≥ 1`, which is trivially satisfied.

## Impact Explanation
This concretely matches: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**. Submitting `N ≥ max_tx_verify_workers` such transactions simultaneously (where `max_tx_verify_workers = max(num_cpus * 3/4, 1)` per `util/app-config/src/configs/tx_pool.rs` line 47) pins all verify workers at near-cycle-limit execution. Legitimate transactions queue behind them indefinitely, degrading tx-pool admission throughput for all honest users.

## Likelihood Explanation
- **Unprivileged**: Any user can submit transactions via RPC or P2P relay.
- **Cheap**: A 1-byte cell dep (minimum ~61 CKB capacity, one-time cost, reusable across unlimited transactions) plus ~300 shannons per transaction at the default `min_fee_rate = 1000 shannons/KB`.
- **Repeatable**: The same cell dep can be referenced by arbitrarily many transactions. The attacker continuously floods the verify queue.
- **No special knowledge required**: The syscall number (2091), register convention, and VM address layout are all publicly documented.

## Recommendation
Charge cycles based on `content_size` (actual bytes transferred into the VM), not `memory_size` (virtual address range reserved). The zero-padding of `memory_size - content_size` bytes is a VM-internal page-initialization cost that should not be exposed as a user-controllable cycle multiplier:

```rust
// script/src/syscalls/load_cell_data.rs, line 163
// Change:
machine.add_cycles_no_checking(transferred_byte_cycles(memory_size))?;
// To:
machine.add_cycles_no_checking(transferred_byte_cycles(content_size))?;
```

Alternatively, cap `memory_size` to `content_size` rounded up to the next page boundary before the `init_pages` call.

## Proof of Concept
1. Deploy a cell dep with 1 byte of data (`0x01`).
2. Write a minimal RISC-V ELF script that:
   - Sets `A0 = 0x20000` (addr in free VM space)
   - Sets `A1 = 64 * 1024 * 1024` (`memory_size = 64 MB`)
   - Sets `A2 = 0` (`content_offset`)
   - Sets `A3 = 1` (`content_size = 1 byte`)
   - Sets `A4 = 0` (index)
   - Sets `A5 = source` (CellDep)
   - Sets `A7 = 2091` (`LOAD_CELL_DATA_AS_CODE_SYSCALL_NUMBER`)
   - Issues `ecall`; repeats 4 times (4 × 16,777,216 = 67,108,864 cycles ≈ 96% of budget)
3. Submit `N ≥ max_tx_verify_workers` such transactions simultaneously.
4. Observe: all verify workers are pinned at near-cycle-limit; the verify queue grows; legitimate transactions stall.

Expected cycle measurement per call: `transferred_byte_cycles(64 MB) = 16,777,216`, confirmed by `script/src/cost_model.rs` lines 10–12. Each transaction is rejected after hitting `max_tx_verify_cycles`, but only after the full `init_pages` work has been performed. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** script/src/syscalls/load_cell_data.rs (L148-163)
```rust
        if content_offset >= cell.len() as u64
            || content_end > cell.len() as u64
            || content_size > memory_size
        {
            machine.set_register(A0, Mac::REG::from_u8(SLICE_OUT_OF_BOUND));
            return Ok(());
        }
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

**File:** util/app-config/src/configs/tx_pool.rs (L46-48)
```rust
pub fn default_max_tx_verify_workers() -> usize {
    std::cmp::max(num_cpus::get() * 3 / 4, 1)
}
```

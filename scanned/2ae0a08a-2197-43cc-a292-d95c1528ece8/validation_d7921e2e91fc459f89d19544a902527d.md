I now have all the information needed to make a definitive assessment. Let me verify the exact cycle math and constraints.

### Title
`load_cell_data_as_code` Charges Cycles on `memory_size` Not `content_size`, Enabling Cheap Verify-Worker Saturation — (`script/src/syscalls/load_cell_data.rs`)

---

### Summary

`load_data_as_code` (syscall 2091) charges `transferred_byte_cycles(memory_size)` unconditionally after a successful `init_pages` call, regardless of how many bytes of actual cell data (`content_size`) were loaded. An unprivileged script author can set `memory_size` to a value far exceeding `content_size` — bounded only by available VM address space — and consume tens of millions of cycles with a single syscall, forcing every tx-pool verify worker that picks up the transaction to run to near-cycle-limit before rejection.

---

### Finding Description

In `load_data_as_code`:

```
// script/src/syscalls/load_cell_data.rs  lines 148–163
if content_offset >= cell.len() as u64
    || content_end > cell.len() as u64
    || content_size > memory_size          // ← only guard: content_size ≤ memory_size
{
    machine.set_register(A0, Mac::REG::from_u8(SLICE_OUT_OF_BOUND));
    return Ok(());
}
machine.memory_mut().init_pages(addr, memory_size, FLAG_EXECUTABLE | FLAG_FREEZED,
    Some(cell.slice(content_offset..content_end)), 0)?;
sc.track_pages(machine, addr, memory_size, &data_piece_id, content_offset)?;
machine.add_cycles_no_checking(transferred_byte_cycles(memory_size))?;  // ← memory_size, not content_size
``` [1](#0-0) 

The only pre-charge guard is `content_size > memory_size` → `SLICE_OUT_OF_BOUND`. There is **no upper bound on `memory_size` itself**. The cycle charge is:

```
// script/src/cost_model.rs  lines 10–12
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    bytes.div_ceil(BYTES_PER_CYCLE)   // BYTES_PER_CYCLE = 4
}
``` [2](#0-1) 

Arithmetic:
- `transferred_byte_cycles(128 MB)` = `128 × 1024 × 1024 / 4` = **33,554,432 cycles** per call
- `DEFAULT_MAX_TX_VERIFY_CYCLES` = `TWO_IN_TWO_OUT_CYCLES × 20` = `3,500,000 × 20` = **70,000,000 cycles** [3](#0-2) [4](#0-3) 

Two calls with `memory_size = RISCV_MAX_MEMORY` consume **67,108,864 cycles** (≈ 96 % of the budget). Even with a more conservative `memory_size` (e.g., 64 MB, leaving room for the script's own code pages and stack), a single call charges **16,777,216 cycles**, and four calls saturate the budget.

The `init_pages` call must succeed for cycles to be charged (cycles are added only after `init_pages` returns `Ok`). The attacker chooses `addr` in the free address space between the loaded ELF code (typically at `0x10000`) and the stack (top ~4 MB of the 128 MB VM), giving a usable window of ~120 MB. `init_pages` fails only if the target pages are already initialized (e.g., `FLAG_FREEZED` pages), which the attacker avoids by targeting the free region. [5](#0-4) 

---

### Impact Explanation

Each crafted transaction forces a verify worker to:
1. Load the script ELF (a few hundred bytes).
2. Execute a handful of RISC-V instructions to set up registers.
3. Issue the `load_cell_data_as_code` ecall, which calls `init_pages` (real page-initialization work) and then charges ~16–33 M cycles.
4. Repeat once or twice until `max_tx_verify_cycles` is hit, then reject.

The verify manager spawns `max_tx_verify_workers` (default: `3/4 × CPU cores`) workers, each processing one transaction at a time. [6](#0-5) 

Submitting `N ≥ max_tx_verify_workers` such transactions simultaneously pins all workers at near-cycle-limit execution, draining the verify queue throughput. Legitimate transactions queue behind them. The fee cost to the attacker is only `min_fee_rate × tx_size` (1000 shannons/KB × ~300 bytes ≈ 300 shannons per transaction), with no fee-per-cycle mechanism in the tx-pool. [7](#0-6) 

---

### Likelihood Explanation

- **Unprivileged**: Any user can submit a transaction via RPC or P2P relay. No key, no special role required.
- **Cheap**: A 1-byte cell dep (minimum ~61 CKB capacity, reusable across many transactions) plus negligible per-transaction fees.
- **Repeatable**: The same cell dep can be referenced by arbitrarily many transactions. The attacker can flood the verify queue continuously.
- **No PoW or consensus bypass needed**: The attack operates entirely within the tx-pool admission path.

---

### Recommendation

Charge cycles based on `content_size` (the actual bytes transferred into the VM), not `memory_size` (the virtual address range reserved). The zero-padding of the remaining `memory_size - content_size` bytes is a VM-internal page-initialization cost that should not be exposed as a user-controllable cycle multiplier. Alternatively, add an explicit upper bound on `memory_size` (e.g., cap it at `content_size` rounded up to the next page boundary, or at a fixed maximum).

```rust
// Proposed fix: charge for content_size, not memory_size
machine.add_cycles_no_checking(transferred_byte_cycles(content_size))?;
``` [8](#0-7) 

---

### Proof of Concept

1. Deploy a cell dep with 1 byte of data (e.g., `0x01`).
2. Write a minimal RISC-V ELF script that:
   - Sets `A0 = 0x20000` (addr, in free VM space above the ELF load address)
   - Sets `A1 = 64 * 1024 * 1024` (memory_size = 64 MB)
   - Sets `A2 = 0` (content_offset)
   - Sets `A3 = 1` (content_size = 1 byte)
   - Sets `A4 = 0` (index)
   - Sets `A5 = source` (CellDep)
   - Sets `A7 = 2091` (LOAD_CELL_DATA_AS_CODE_SYSCALL_NUMBER)
   - Issues `ecall`; repeats 4 times (4 × 16,777,216 = 67,108,864 cycles ≈ 96 % of budget)
3. Submit 8–16 such transactions simultaneously (matching `max_tx_verify_workers`).
4. Observe: all verify workers are pinned at near-cycle-limit; `verify_queue_size` grows; legitimate transactions stall.

Expected cycle measurement per call: `transferred_byte_cycles(64 MB) = 16,777,216`, confirmed by the formula at `script/src/cost_model.rs:10–12`. [9](#0-8) [10](#0-9)

### Citations

**File:** script/src/syscalls/load_cell_data.rs (L101-166)
```rust
    fn load_data_as_code<Mac: SupportMachine>(&self, machine: &mut Mac) -> Result<(), VMError> {
        let addr = machine.registers()[A0].to_u64();
        let memory_size = machine.registers()[A1].to_u64();
        let content_offset = machine.registers()[A2].to_u64();
        let content_size = machine.registers()[A3].to_u64();
        let index = machine.registers()[A4].to_u64();
        let mut source = machine.registers()[A5].to_u64();
        // To keep compatible with the old behavior. When Source is wrong, a
        // Vm internal error should be returned.
        if let Source::Group(_) = Source::parse_from_u64(source)? {
            source = source & SOURCE_ENTRY_MASK | SOURCE_GROUP_FLAG;
        } else {
            source &= SOURCE_ENTRY_MASK;
        }
        let data_piece_id = match DataPieceId::try_from((source, index, 0)) {
            Ok(id) => id,
            Err(_) => {
                machine.set_register(A0, Mac::REG::from_u8(INDEX_OUT_OF_BOUND));
                return Ok(());
            }
        };
        let mut sc = self
            .snapshot2_context
            .lock()
            .map_err(|e| VMError::Unexpected(e.to_string()))?;
        // We are using 0..u64::MAX to fetch full cell, there is
        // also no need to keep the full length value. Since cell's length
        // is already full length.
        let (cell, _) = match sc.load_data(&data_piece_id, 0, u64::MAX) {
            Ok(val) => {
                if content_size == 0 {
                    (Bytes::new(), val.1)
                } else {
                    val
                }
            }
            Err(VMError::SnapshotDataLoadError) => {
                // This comes from TxData results in an out of bound error, to
                // mimic current behavior, we would return INDEX_OUT_OF_BOUND error.
                machine.set_register(A0, Mac::REG::from_u8(INDEX_OUT_OF_BOUND));
                return Ok(());
            }
            Err(e) => return Err(e),
        };
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
        machine.memory_mut().init_pages(
            addr,
            memory_size,
            FLAG_EXECUTABLE | FLAG_FREEZED,
            Some(cell.slice((content_offset as usize)..(content_end as usize))),
            0,
        )?;
        sc.track_pages(machine, addr, memory_size, &data_piece_id, content_offset)?;
        machine.add_cycles_no_checking(transferred_byte_cycles(memory_size))?;
        machine.set_register(A0, Mac::REG::from_u8(SUCCESS));
        Ok(())
    }
```

**File:** script/src/cost_model.rs (L7-12)
```rust
pub const BYTES_PER_CYCLE: u64 = 4;

/// Calculates how many cycles spent to load the specified number of bytes.
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
```

**File:** spec/src/consensus.rs (L70-70)
```rust
pub const TWO_IN_TWO_OUT_CYCLES: Cycle = 3_500_000;
```

**File:** util/app-config/src/legacy/tx_pool.rs (L14-14)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

**File:** tx-pool/src/verify_mgr.rs (L179-188)
```rust
        let worker_num = service.tx_pool_config.max_tx_verify_workers;
        let workers: Vec<_> = (0..worker_num)
            .map({
                let tasks = Arc::clone(&service.verify_queue);
                let signal_exit = signal_exit.clone();
                move |idx| {
                    let role = if idx == 0 && worker_num > 1 {
                        WorkerRole::OnlySmallCycleTx
                    } else {
                        WorkerRole::SubmitTimeFirst
```

**File:** util/app-config/src/configs/tx_pool.rs (L20-24)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
    #[serde(default = "default_max_tx_verify_workers")]
    pub max_tx_verify_workers: usize,
```

I have verified all four code paths against the actual source files. The claims are accurate.

All four code paths are verified against the actual source. The finding is accurate and concrete:

- `ExecV2::ecall` performs no bounds validation [1](#0-0) 
- `process_message_box` propagates `sc.load_data` errors with `?` as hard VM termination [2](#0-1) 
- `Spawn::ecall` explicitly catches `SnapshotDataLoadError` and checks bounds before queuing [3](#0-2) 
- `Exec::ecall` explicitly returns `SLICE_OUT_OF_BOUND` for both out-of-bounds cases [4](#0-3) 
- `LoadCellData` also catches `SnapshotDataLoadError` and converts it gracefully, confirming the codebase-wide pattern [5](#0-4) 

---

Audit Report

## Title
ExecV2 Missing Bounds Check Causes Hard VM Termination Instead of `SLICE_OUT_OF_BOUND` — (`script/src/syscalls/exec_v2.rs`, `script/src/scheduler.rs`)

## Summary
`ExecV2::ecall` performs no bounds validation on `offset`/`length` before queuing `Message::ExecV2`. The scheduler's `process_message_box` then calls `sc.load_data` with the raw user-supplied values and propagates any resulting error via `?`, causing hard VM termination instead of returning `SLICE_OUT_OF_BOUND` (error code 3) to the script. Both `Exec::ecall` and `Spawn::ecall` explicitly return `SLICE_OUT_OF_BOUND` for the same condition, establishing a clear invariant that ExecV2 violates.

## Finding Description
**Root cause — `ExecV2::ecall` (`exec_v2.rs` lines 46–67):** `offset` and `length` are read from registers and placed into `DataLocation` with no validation. The message is queued and `VMError::Yield` is returned unconditionally.

**Error propagation — `Scheduler::process_message_box` (`scheduler.rs` lines 496–504):** The `Message::ExecV2` handler calls:
```rust
sc.load_data(
    &args.location.data_piece_id,
    args.location.offset,
    args.location.length,
)?
.0
```
The `?` operator propagates any error from `sc.load_data` — including `VMError::SnapshotDataLoadError` for invalid data piece IDs, or a hard error for out-of-bounds offset/length — directly up through `iterate_process_results` → `iterate_inner` → `iterate_outer` → `run`, terminating script execution without setting A0.

**Contrast with correct implementations:**
- `Spawn::ecall` (`spawn.rs` lines 112–131): calls `sc.load_data(&data_piece_id, 0, 0)` to probe full length, catches `SnapshotDataLoadError` → `INDEX_OUT_OF_BOUND`, then explicitly checks `offset >= full_length` and `offset+length > full_length` → `SLICE_OUT_OF_BOUND` before queuing.
- `Exec::ecall` (`exec.rs` lines 138–152): explicitly checks `offset >= data_size` and `end > data_size`, returning `SLICE_OUT_OF_BOUND` in both cases.
- `LoadCellData` (`load_cell_data.rs` lines 79–85): also catches `SnapshotDataLoadError` and converts it to `INDEX_OUT_OF_BOUND`.

The pattern is codebase-wide: every other syscall that accesses data by offset/length performs explicit bounds checking and returns a graceful error code. ExecV2 is the sole exception.

## Impact Explanation
This is an incorrect implementation of a CKB-VM system syscall, matching the allowed impact: **"High (10001–15000 points). Incorrect implementation or behavior of CKB-VM or system scripts."**

A `ScriptVersion::V2` script that calls ExecV2 with `offset >= cell_data_length` (or `offset+length > cell_data_length`) and expects to receive `SLICE_OUT_OF_BOUND` in A0 — to implement fallback or error-handling logic — will instead cause the entire scheduler to terminate with a hard VM error. The transaction fails at the node level rather than allowing the script to handle the condition. Any multi-script protocol relying on graceful ExecV2 failure semantics is broken by this inconsistency.

## Likelihood Explanation
ExecV2 is available to any unprivileged script author deploying a `ScriptVersion::V2` script. Triggering the bug requires only crafting a transaction where the script calls ExecV2 with `bounds = (cell_data_length + 1) << 32` (offset one past the end, length=0). This is trivially constructable, locally testable, and requires no special privileges or external conditions.

## Recommendation
In `process_message_box` for `Message::ExecV2` (`scheduler.rs` lines 496–504), replicate the bounds-checking pattern from `Spawn::ecall`:
1. Call `sc.load_data(&args.location.data_piece_id, 0, 0)` to obtain `full_length`.
2. Catch `SnapshotDataLoadError` → set A0 to `INDEX_OUT_OF_BOUND` on the calling VM and `continue`.
3. Check `offset >= full_length` → set A0 to `SLICE_OUT_OF_BOUND` and `continue`.
4. Check `offset + length > full_length` → set A0 to `SLICE_OUT_OF_BOUND` and `continue`.
5. Only then call `sc.load_data` with the validated offset/length.

Alternatively, perform the bounds check inside `ExecV2::ecall` before queuing the message, mirroring the approach used in `Exec::ecall`. This requires passing the `snapshot2_context` into `ExecV2` (as `Spawn` already does).

## Proof of Concept
1. Deploy a `ScriptVersion::V2` lock script that:
   - Reads the length of cell dep 0's data (e.g., via `load_cell_data` with `size=0`).
   - Calls ExecV2 with `index=0`, `source=CellDep`, `place=CellData`, and `bounds = (data_length + 1) << 32` (offset one past the end, length=0).
   - After ExecV2 returns, checks if A0 == 3 (`SLICE_OUT_OF_BOUND`); if so, exits with code 0 (success).
2. Submit a transaction using this lock script.
3. **Expected (per invariant from `Exec::ecall` and `Spawn::ecall`):** A0 = 3, script exits 0, transaction validates.
4. **Actual:** `process_message_box` propagates the error from `sc.load_data` as a hard `VMError`; the scheduler returns an error; transaction verification fails with a VM-level error rather than a script exit code.

### Citations

**File:** script/src/syscalls/exec_v2.rs (L46-67)
```rust
        let bounds = machine.registers()[A3].to_u64();
        let offset = bounds >> 32;
        let length = bounds as u32 as u64;

        let argc = machine.registers()[A4].to_u64();
        let argv = machine.registers()[A5].to_u64();
        self.message_box
            .lock()
            .map_err(|e| VMError::Unexpected(e.to_string()))?
            .push(Message::ExecV2(
                self.id,
                ExecV2Args {
                    location: DataLocation {
                        data_piece_id,
                        offset,
                        length,
                    },
                    argc,
                    argv,
                },
            ));
        Err(VMError::Yield)
```

**File:** script/src/scheduler.rs (L496-504)
```rust
                    let program = {
                        let sc = old_context.snapshot2_context.lock().expect("lock");
                        sc.load_data(
                            &args.location.data_piece_id,
                            args.location.offset,
                            args.location.length,
                        )?
                        .0
                    };
```

**File:** script/src/syscalls/spawn.rs (L112-131)
```rust
        let (_, full_length) = match sc.load_data(&data_piece_id, 0, 0) {
            Ok(val) => val,
            Err(VMError::SnapshotDataLoadError) => {
                // This comes from TxData results in an out of bound error, to
                // mimic current behavior, we would return INDEX_OUT_OF_BOUND error.
                machine.set_register(A0, Mac::REG::from_u8(INDEX_OUT_OF_BOUND));
                return Ok(true);
            }
            Err(e) => return Err(e),
        };
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

**File:** script/src/syscalls/load_cell_data.rs (L79-85)
```rust
                    Err(VMError::SnapshotDataLoadError) => {
                        // This comes from TxData results in an out of bound error, to
                        // mimic current behavior, we would return INDEX_OUT_OF_BOUND error.
                        machine.set_register(A0, Mac::REG::from_u8(INDEX_OUT_OF_BOUND));
                        return Ok(());
                    }
                    Err(e) => return Err(e),
```

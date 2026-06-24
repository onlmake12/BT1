Audit Report

## Title
ExecV2 Missing Bounds Check Causes Hard VM Termination Instead of `SLICE_OUT_OF_BOUND` — (`script/src/scheduler.rs`, `script/src/syscalls/exec_v2.rs`)

## Summary
`ExecV2::ecall` performs no bounds validation on `offset`/`length` before queuing `Message::ExecV2`. The scheduler's `process_message_box` then calls `sc.load_data` with the raw user-supplied values and propagates any resulting error via `?`, causing hard VM termination instead of returning `SLICE_OUT_OF_BOUND` (error code 3) to the script. Both `Exec::ecall` and `Spawn::ecall` explicitly return `SLICE_OUT_OF_BOUND` for the same condition, establishing a clear invariant that ExecV2 breaks.

## Finding Description
In `ExecV2::ecall` (`exec_v2.rs` lines 46–67), `offset` and `length` are read from registers and placed into `DataLocation` with no bounds validation before queuing `Message::ExecV2` and returning `Err(VMError::Yield)`. [1](#0-0) 

In `Scheduler::process_message_box` (`scheduler.rs` lines 496–504), the `Message::ExecV2` handler calls `sc.load_data` with the raw user-supplied `offset` and `length`, and the `?` operator propagates any error — including `VMError::SnapshotDataLoadError` — as a hard error up through the entire scheduler call stack. [2](#0-1) 

By contrast, `Spawn::ecall` (`spawn.rs` lines 112–131) first calls `sc.load_data(&data_piece_id, 0, 0)` to probe the full length, catches `SnapshotDataLoadError` and converts it to `INDEX_OUT_OF_BOUND`, then explicitly checks `offset >= full_length` and `offset+length > full_length`, returning `SLICE_OUT_OF_BOUND` in both cases before ever queuing the message. [3](#0-2) 

`Exec::ecall` (`exec.rs` lines 138–152) similarly checks `offset >= data_size` and `offset+length > data_size`, returning `SLICE_OUT_OF_BOUND` in both cases. [4](#0-3) 

The exploit path is: script calls ExecV2 with `bounds = (cell_data_length + 1) << 32` → `ExecV2::ecall` queues the message without validation → `process_message_box` calls `sc.load_data` with out-of-bounds offset → `sc.load_data` returns `VMError::SnapshotDataLoadError` → `?` propagates it as a hard error → scheduler terminates with an error → transaction verification fails. The script never receives `SLICE_OUT_OF_BOUND` in A0 and cannot handle the condition gracefully.

## Impact Explanation
This is an incorrect implementation of CKB-VM system syscall behavior, matching the allowed impact: **"Incorrect implementation or behavior of CKB-VM or system scripts" — High (10001–15000 points)**. Any `ScriptVersion::V2` script that calls ExecV2 with an out-of-bounds offset and expects to receive `SLICE_OUT_OF_BOUND` in A0 (to implement fallback logic) will instead cause the entire VM to terminate with a hard error, making the transaction fail verification. This breaks the documented and implemented invariant of the exec-family syscalls.

## Likelihood Explanation
Any unprivileged script author deploying a `ScriptVersion::V2` script can trigger this. The trigger requires only crafting a transaction where the script calls ExecV2 with `offset >= cell_data_length`. This is trivially constructable and locally testable with no special privileges or external conditions required.

## Recommendation
In `process_message_box` for `Message::ExecV2`, replicate the bounds-checking pattern from `Spawn::ecall`:
1. Call `sc.load_data(&data_piece_id, 0, 0)` to obtain `full_length`.
2. Catch `SnapshotDataLoadError` and set A0 to `INDEX_OUT_OF_BOUND`, then `continue`.
3. Check `offset >= full_length` → set A0 to `SLICE_OUT_OF_BOUND`, then `continue`.
4. Check `offset + length > full_length` → set A0 to `SLICE_OUT_OF_BOUND`, then `continue`.
5. Only then call `sc.load_data` with the validated offset/length.

Alternatively, perform the bounds check inside `ExecV2::ecall` before queuing the message, mirroring the approach used in `Exec::ecall` — though this requires access to `snapshot2_context` in the ecall handler.

## Proof of Concept
1. Deploy a `ScriptVersion::V2` lock script that:
   - Calls ExecV2 with `index=0` (a valid cell dep), `source=CellDep`, `place=CellData`, and `bounds = (cell_data_length + 1) << 32` (offset one past the end, length=0).
   - After the ExecV2 call returns, checks if A0 == `SLICE_OUT_OF_BOUND` (3); if so, exits with code 0 (success).
2. Submit a transaction using this lock script.
3. **Expected (per invariant):** A0 = 3, script exits 0, transaction validates.
4. **Actual:** `process_message_box` propagates `VMError::SnapshotDataLoadError` as a hard error; the scheduler returns an error; transaction verification fails with a VM error rather than allowing the script to handle the condition.

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

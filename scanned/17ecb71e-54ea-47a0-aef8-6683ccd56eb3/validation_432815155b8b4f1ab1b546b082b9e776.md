I need to carefully trace the ExecV2 code path and compare it with both the old `Exec::ecall` and `Spawn::ecall` to verify the claimed invariant violation.

**Step 1: ExecV2::ecall — no bounds checking at all**

`ExecV2::ecall` simply parses `offset` and `length` from registers and queues the message with no validation: [1](#0-0) 

The function pushes `Message::ExecV2` and returns `Err(VMError::Yield)`. No bounds check on `offset` or `length` whatsoever.

**Step 2: process_message_box — sc.load_data called with raw user-supplied offset/length, error propagated with `?`** [2](#0-1) 

The `?` on `sc.load_data(...)` propagates any error — including `VMError::SnapshotDataLoadError` — directly up through `process_message_box` → `iterate_process_results` → `iterate_inner` → `iterate_outer` → `run`. There is no catch, no conversion to `SLICE_OUT_OF_BOUND`.

**Step 3: Spawn::ecall — explicit SnapshotDataLoadError catch AND explicit bounds checking** [3](#0-2) 

Spawn (a) catches `SnapshotDataLoadError` and converts it to `INDEX_OUT_OF_BOUND`, and (b) explicitly checks `offset >= full_length` and `offset+length > full_length`, returning `SLICE_OUT_OF_BOUND` in both cases.

**Step 4: Old Exec::ecall — same invariant, explicit SLICE_OUT_OF_BOUND for out-of-bounds** [4](#0-3) 

The original `Exec::ecall` explicitly returns `SLICE_OUT_OF_BOUND` for both `offset >= data_size` and `offset+length > data_size`. This confirms the invariant is intentional across exec-family syscalls.

**Step 5: What does sc.load_data return for out-of-bounds offset+length?**

The Spawn code calls `sc.load_data(&data_piece_id, 0, 0)` to probe the full length, then does its own bounds checking *before* queuing the message — it never calls `sc.load_data` with the actual user-supplied offset/length. This design choice is deliberate: it avoids relying on `sc.load_data` to handle out-of-bounds gracefully. The ExecV2 `process_message_box` path does the opposite — it passes raw user-supplied offset/length directly to `sc.load_data`. If `sc.load_data` returns `SnapshotDataLoadError` (as the Spawn comment documents for out-of-bound TxData access), the error propagates as a hard VM termination.

**Conclusion**

The discrepancy is real and concrete:

| Syscall | Bounds check location | Out-of-bounds result to script |
|---|---|---|
| `Exec::ecall` | Inside ecall, before reset | `SLICE_OUT_OF_BOUND` (A0=3) |
| `Spawn::ecall` | Inside ecall, before queuing | `SLICE_OUT_OF_BOUND` (A0=3) |
| `ExecV2` (via `process_message_box`) | **None** | Hard `VMError` propagation |

A script under `ScriptVersion::V2` that calls ExecV2 with `offset = cell_data_length + 1` will not receive `SLICE_OUT_OF_BOUND` in A0. Instead, the scheduler's `process_message_box` propagates the error from `sc.load_data` as a hard VM termination, bypassing any in-script error handling. The invariant established by both `Exec::ecall` and `Spawn::ecall` — that out-of-bounds data access returns `SLICE_OUT_OF_BOUND` — is broken for ExecV2.

---

### Title
ExecV2 Missing Bounds Check in `process_message_box` Returns Hard VM Error Instead of `SLICE_OUT_OF_BOUND` — (`script/src/scheduler.rs`, `script/src/syscalls/exec_v2.rs`)

### Summary
`ExecV2::ecall` performs no bounds validation on `offset`/`length` before queuing `Message::ExecV2`. The scheduler's `process_message_box` then calls `sc.load_data` with the raw user-supplied values and propagates any resulting error with `?`, causing a hard VM termination instead of returning `SLICE_OUT_OF_BOUND` (error code 3) to the script. Both `Exec::ecall` and `Spawn::ecall` explicitly return `SLICE_OUT_OF_BOUND` for the same condition.

### Finding Description
In `ExecV2::ecall` (`exec_v2.rs` lines 46–67), `offset` and `length` are read from registers and placed into `DataLocation` without any bounds validation. The message is queued and `VMError::Yield` is returned.

In `Scheduler::process_message_box` (`scheduler.rs` lines 496–503), the handler for `Message::ExecV2` calls:
```rust
sc.load_data(
    &args.location.data_piece_id,
    args.location.offset,
    args.location.length,
)?
.0
```
The `?` operator propagates `VMError::SnapshotDataLoadError` (or any other error from `sc.load_data`) as a hard error up through the entire scheduler call stack, terminating script execution without setting A0.

By contrast:
- `Exec::ecall` (`exec.rs` lines 139–152) explicitly checks `offset >= data_size` and `offset+length > data_size`, returning `SLICE_OUT_OF_BOUND` in both cases.
- `Spawn::ecall` (`spawn.rs` lines 112–131) calls `sc.load_data` with `offset=0, length=0` to probe the full length, catches `SnapshotDataLoadError`, and explicitly checks bounds before queuing.

### Impact Explanation
Any `ScriptVersion::V2` script that calls ExecV2 with an out-of-bounds `offset` or `offset+length` and expects to receive `SLICE_OUT_OF_BOUND` in A0 (to implement fallback logic) will instead cause the entire VM to terminate with a hard error. The transaction verification fails with a VM error rather than allowing the script to handle the condition gracefully. Multi-script protocols that rely on graceful exec failure handling are broken.

### Likelihood Explanation
The ExecV2 syscall is available to any unprivileged script author deploying a `ScriptVersion::V2` script. The trigger requires only crafting a transaction where the script calls ExecV2 with `offset >= cell_data_length`. This is trivially constructable and locally testable.

### Recommendation
In `process_message_box` for `Message::ExecV2`, replicate the bounds-checking pattern from `Spawn::ecall`:
1. Call `sc.load_data(&data_piece_id, 0, 0)` to obtain `full_length`.
2. Catch `SnapshotDataLoadError` and set A0 to `INDEX_OUT_OF_BOUND`.
3. Check `offset >= full_length` → set A0 to `SLICE_OUT_OF_BOUND`.
4. Check `offset + length > full_length` → set A0 to `SLICE_OUT_OF_BOUND`.
5. Only then call `sc.load_data` with the validated offset/length.

Alternatively, perform the bounds check inside `ExecV2::ecall` before queuing the message, mirroring the approach used in `Exec::ecall`.

### Proof of Concept
1. Deploy a `ScriptVersion::V2` lock script that:
   - Calls ExecV2 with `index=0` (a valid cell dep), `source=CellDep`, `place=CellData`, and `bounds = (cell_data_length + 1) << 32` (offset one past the end, length=0).
   - After the ExecV2 call returns, checks if A0 == `SLICE_OUT_OF_BOUND` (3); if so, exits with code 0 (success).
2. Submit a transaction using this lock script.
3. **Expected (per invariant):** A0 = 3, script exits 0, transaction validates.
4. **Actual:** `process_message_box` propagates `VMError::SnapshotDataLoadError` (or equivalent) as a hard error; the scheduler returns an error; transaction verification fails.

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

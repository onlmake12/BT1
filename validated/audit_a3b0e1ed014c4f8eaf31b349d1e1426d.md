The key code is at line 595 of `script/src/scheduler.rs`. Let me analyze the exact check:

### Title
Off-by-One in `MAX_FDS` Guard Allows 65 File Descriptors Instead of 64 - (`script/src/scheduler.rs`)

---

### Summary

The `Message::Pipe` handler in `Scheduler::process_message_box` checks `self.fds.len() as u64 >= MAX_FDS` before inserting **two** new file descriptors. Because a pipe always creates two fds atomically, the guard must fire when `fds.len() >= MAX_FDS - 1` (i.e., `>= 63`). The current `>= 64` threshold allows the insertion to proceed when exactly 63 fds are present, pushing the total to 65 and silently violating the documented invariant.

---

### Finding Description

`MAX_FDS` is defined as `64`: [1](#0-0) 

The guard in the `Message::Pipe` arm is: [2](#0-1) 

When `self.fds.len() == 63`, the expression `63 as u64 >= 64` evaluates to **false**, so the guard is skipped. The two-fd insertion at lines 604–605 then executes unconditionally, bringing `fds.len()` to **65**. The next pipe call sees `65 >= 64 == true` and is correctly rejected, so the ceiling is 65, not unbounded — but it is one pipe (two fds) beyond the documented limit.

The correct guard must account for the fact that a successful pipe call always inserts **two** entries:

```rust
// Correct: reject if there is no room for two more fds
if self.fds.len() as u64 + 2 > MAX_FDS { … }
// Equivalently:
if self.fds.len() as u64 >= MAX_FDS - 1 { … }
```

---

### Impact Explanation

An unprivileged script author can hold 65 live file descriptors in the scheduler state instead of the guaranteed maximum of 64. This breaks the invariant that `fds.len() <= MAX_FDS` at all times. The excess fd pair is fully functional (readable/writable), meaning the script obtains one extra communication channel beyond the resource cap. The impact is bounded to a single extra pipe per script execution context; it does not cascade to other transactions or the broader network.

---

### Likelihood Explanation

The exploit requires only ordinary `ckb_pipe()` and `ckb_close()` syscalls available to any script running under `ScriptVersion::V2`. No privileged access, no PoW, no key material, and no network-level attack is needed. The sequence is deterministic and reproducible in a unit test.

---

### Recommendation

Change the guard to check whether room for **two** new entries exists before proceeding:

```rust
// script/src/scheduler.rs — Message::Pipe arm
if self.fds.len() as u64 + 2 > MAX_FDS {
    // return MAX_FDS_CREATED
}
```

This ensures `fds.len()` never exceeds `MAX_FDS` after any pipe call.

---

### Proof of Concept

```
1. Call ckb_pipe() × 31  →  fds.len() == 62
2. Call ckb_close(fd)    →  fds.len() == 61
3. Call ckb_pipe()       →  fds.len() == 63
4. Call ckb_pipe()       →  guard: 63 >= 64 == false → insert 2 fds
   assert fds.len() == 65   ← invariant violated
5. Call ckb_pipe()       →  guard: 65 >= 64 == true  → MAX_FDS_CREATED (correctly rejected)
```

Step 4 confirms that `fds.len()` reaches 65, one pipe (two fds) beyond `MAX_FDS = 64`, with both excess fds fully operational.

### Citations

**File:** script/src/scheduler.rs (L38-38)
```rust
pub const MAX_FDS: u64 = 64;
```

**File:** script/src/scheduler.rs (L594-605)
```rust
                Message::Pipe(vm_id, args) => {
                    if self.fds.len() as u64 >= MAX_FDS {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(MAX_FDS_CREATED));
                        continue;
                    }
                    let (p1, p2, slot) = Fd::create(self.next_fd_slot);
                    self.next_fd_slot = slot;
                    self.fds.insert(p1, vm_id);
                    self.fds.insert(p2, vm_id);
```

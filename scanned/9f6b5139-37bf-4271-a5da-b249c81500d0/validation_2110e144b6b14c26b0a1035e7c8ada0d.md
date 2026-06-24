Audit Report

## Title
ELF Parsing and Memory Allocation Performed Before Cycle-Limit Enforcement in VM Boot Path — (`script/src/scheduler.rs`)

## Summary
In CKB's `Scheduler`, when a new VM is booted via `boot_vm()` — either for the root script or via a `ckb_spawn` syscall — the expensive operations of ELF parsing (`parse_elf`) and program loading into VM memory (`load_program_with_metadata`) execute with no cycle pre-check. The cycle cost is charged to the new machine's internal counter via `add_cycles_no_checking`, which does not enforce the limit. Limit enforcement only occurs in the next scheduler iteration when `set_max_cycles` is called. An unprivileged attacker can craft transactions that force full ELF parsing and heap allocation before the cycle check fires, making sustained CPU/memory pressure cheaper than the cycle model intends.

## Finding Description
**Root VM path:** `run()` calls `boot_root_vm_if_needed()` at L292 before `limit_cycles` is even extracted from `mode` (L294–297) and before the cycle-enforcement loop begins (L299–301). The root ELF is fully parsed and loaded into VM memory with zero cycle budget enforcement.

**Spawn path call chain:** `iterate_inner()` → `iterate_process_results()` → `process_message_box()` → `boot_vm()` → `load_vm_program()`.

Inside `boot_vm()` (L1015–1039):
- `create_dummy_vm()` allocates a new RISC-V machine initialized with `max_cycles = u64::MAX` (L1083–1084), meaning no limit is active during boot.
- `sc.load_data(...)` reads the full ELF binary.
- `load_vm_program()` (L1042–1075) calls `parse_elf`, `load_program_with_metadata` (heap allocation proportional to ELF size), and `sc.mark_program` — all before any cycle check.

The cycle charge is applied only after all this work:
```rust
machine.inner_mut().add_cycles_no_checking(transferred_byte_cycles(bytes))?;
```
`add_cycles_no_checking` accumulates cycles on the new machine but does **not** enforce the limit. The limit is only enforced in the next iteration:
```rust
vm.inner_mut().set_max_cycles(limit_cycles);
let result = vm.run();
```
`iterate_outer()` checks `limit_cycles.checked_sub(self.iteration_cycles)` (L424–426), but `iteration_cycles` only contains cycles from the spawning VM's execution — not the new machine's pre-loaded ELF cost. If the new machine's pre-loaded cycle count already exceeds `limit_cycles`, it fails immediately on its first instruction, but all expensive setup work has already completed.

**Existing guards are insufficient:** `MAX_VMS_COUNT = 16` limits spawned VMs per transaction but does not prevent the per-spawn ELF loading cost from being incurred before the check. The flat fees `SPAWN_EXTRA_CYCLES_BASE = 100_000` and `SPAWN_YIELD_CYCLES_BASE = 800` do not cover the variable cost of loading a large ELF. For a 1 MB ELF, `transferred_byte_cycles(1_048_576) = 262_144` cycles, which can exceed the remaining budget after the flat fees are charged.

## Impact Explanation
This is a **High** severity finding matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker submitting transactions with `max_cycles` tuned to the gap between the flat spawn fee and the ELF loading cost forces the node to perform O(ELF size) heap allocation and ELF segment mapping per transaction, while the transaction is rejected for cycle exhaustion. The node's tx-pool verification workers and block verification path both execute this code path. Repeated submission of such transactions sustains CPU and memory pressure disproportionate to the cycle budget the attacker nominally consumes.

## Likelihood Explanation
The attack requires only the ability to submit transactions to the tx-pool, which is open to any network peer with no special privileges, keys, or hashpower. The attacker deploys a large cell dep ELF once (a normal on-chain operation) and then submits arbitrarily many transactions referencing it. The attack is repeatable, cheap relative to the node work induced, and requires no victim mistakes or external dependencies.

## Recommendation
Before calling `boot_vm()` (or at the start of `load_vm_program()`), verify that the remaining cycle budget covers at least `transferred_byte_cycles(program_size)`. Concretely:

1. In `process_message_box` for `Message::Spawn`, compute the expected `transferred_byte_cycles` from the known program size (already available after `sc.load_data`) and check it against the remaining cycle budget before proceeding to `boot_vm`.
2. Alternatively, charge `transferred_byte_cycles` to `iteration_cycles` (the scheduler-level counter checked against the limit in `iterate_outer`) rather than to the new machine's unchecked counter, so the cost is enforced in the same iteration that triggers the spawn.
3. Apply the same fix to the root VM boot path in `boot_root_vm_if_needed`, ensuring the ELF loading cost is checked against `limit_cycles` before `boot_vm` is called.

## Proof of Concept
1. Deploy a cell dep containing a valid ELF binary of ~1 MB.
2. Submit a transaction whose lock script is a minimal program that immediately calls `ckb_spawn(cell_dep_index, ...)` targeting the large ELF.
3. Set `max_cycles` to `~150_000` (above `SPAWN_EXTRA_CYCLES_BASE + SPAWN_YIELD_CYCLES_BASE = 100_800`, below `100_800 + transferred_byte_cycles(1_048_576) = 362_944`).
4. The node executes the spawning script, issues the spawn syscall (charges 100,800 cycles, yields), then enters `process_message_box` → `boot_vm`:
   - `create_dummy_vm` allocates the machine with `max_cycles = u64::MAX`
   - `load_data` reads the 1 MB ELF
   - `parse_elf` parses it
   - `load_program_with_metadata` maps it into VM memory
   - `add_cycles_no_checking(262_144)` charges to the new machine — no limit enforced
5. Next iteration: `set_max_cycles(remaining ~49_200)` is called; the machine already has 262,144 cycles accumulated; it fails immediately with `CyclesExceeded`.
6. Transaction is rejected, but the node has performed full ELF parsing and memory allocation for the spawned VM.
7. Repeat with many such transactions to sustain CPU/memory pressure on the node.
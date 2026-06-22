Looking at the actual code carefully to determine if this is a real, exploitable vulnerability.

### Title
Uncharged O(n) `header_deps` Linear Scan in `load_header` Syscall Breaks Cycle-to-CPU-Work Invariant — (`script/src/syscalls/load_header.rs`)

---

### Summary

The `load_header` syscall performs a full linear scan of `header_deps` in host (Rust) code on every invocation for `Source::Transaction(Input)` and `Source::Transaction(CellDep)`. This scan is never charged VM cycles. A script can call the syscall in a tight loop, causing O(N × loop_count) host CPU work while consuming only O(loop_count) VM cycles, where N is the number of `header_deps`. This breaks the invariant that the cycle limit bounds node CPU work.

---

### Finding Description

In `load_header`, the private `load_header` method performs a membership check by iterating the entire `header_deps` vector: [1](#0-0) 

The code's own comment acknowledges this is a linear scan: [2](#0-1) 

In the `ecall` handler, cycles are only charged for bytes written to VM memory — and only on the *success* path. When the scan exhausts all `header_deps` without a match, the syscall returns `ITEM_MISSING` via an early return, and `add_cycles_no_checking` is never called: [3](#0-2) 

The cost model charges only for transferred bytes: [4](#0-3) 

The linear scan work — iterating and comparing N × 32-byte hashes in host code — is entirely invisible to the cycle counter.

---

### Impact Explanation

The block cycle limit is `MAX_BLOCK_CYCLES = 3,500,000,000`: [5](#0-4) 

The block byte limit is `MAX_BLOCK_BYTES = 597,000 bytes`: [6](#0-5) 

Each `header_dep` is a 32-byte hash, so a single transaction can carry up to ~15,000–18,000 `header_deps` before hitting the byte limit. There is no separate `MAX_HEADER_DEPS` constant in the codebase.

A malicious script can call `load_header(Source::Transaction(Input), 0)` in a tight loop. Each call costs ~5 RISC-V instructions (~5 cycles) but triggers a full O(N) scan in host code. With N = 15,000 and a 3.5 billion cycle budget, the script can make ~700 million calls, each scanning 15,000 × 32 bytes = 480,000 bytes. Total host work: ~336 terabytes of comparisons — completely unbounded relative to the cycle limit.

Nodes with slower hardware will take far longer to verify such a block than the cycle limit implies, causing them to fall behind or reject valid blocks, producing consensus deviation.

---

### Likelihood Explanation

The attack requires only:
1. Submitting a transaction via the standard `send_transaction` RPC or P2P relay — no privileged access.
2. A lock/type script that calls `load_header` in a loop (trivially written in RISC-V).
3. Filling `header_deps` with valid on-chain block hashes (publicly available) and inputs whose `block_hash` is not among them.

All inputs are attacker-controlled and reachable through normal transaction submission. The `SizeVerifier` enforces the byte limit but does not limit `header_deps` count independently: [7](#0-6) 

---

### Recommendation

Charge cycles proportional to the number of `header_deps` scanned on every `load_header` invocation, regardless of whether the lookup succeeds. Alternatively, replace the linear scan with a `HashSet` built once per script group execution, making each lookup O(1) with a one-time O(N) setup cost that is charged upfront.

---

### Proof of Concept

1. Mine ~15,000 blocks to obtain 15,000 distinct valid block hashes `H[0..14999]`.
2. Construct a transaction with:
   - `header_deps = [H[0], H[1], ..., H[14999]]` (15,000 entries × 32 bytes = 480,000 bytes, within the 597,000-byte block limit).
   - One input cell whose `transaction_info.block_hash` is a hash **not** in `header_deps`.
   - A lock script that executes: `loop { syscall(LOAD_HEADER, 0, Source::Transaction(Input)); }` until cycles are exhausted.
3. Submit the transaction and measure wall-clock time for script verification.
4. Assert that wall-clock time far exceeds what the cycle limit implies for equivalent on-VM work, demonstrating that the cycle limit does not bound actual CPU time.

### Citations

**File:** script/src/syscalls/load_header.rs (L32-35)
```rust
    // This can only be used for liner search
    // header_deps: Byte32Vec,
    // resolved_inputs: &'a [CellMeta],
    // resolved_cell_deps: &'a [CellMeta],
```

**File:** script/src/syscalls/load_header.rs (L61-64)
```rust
        if self
            .header_deps()
            .into_iter()
            .any(|hash| &hash == block_hash)
```

**File:** script/src/syscalls/load_header.rs (L163-176)
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
        machine.set_register(A0, Mac::REG::from_u8(return_code));
```

**File:** script/src/cost_model.rs (L10-12)
```rust
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
```

**File:** spec/src/consensus.rs (L83-83)
```rust
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
```

**File:** spec/src/consensus.rs (L84-84)
```rust
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** verification/src/transaction_verifier.rs (L314-325)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let size = self.transaction.data().serialized_size_in_block() as u64;
        if size <= self.block_bytes_limit {
            Ok(())
        } else {
            Err(TransactionError::ExceededMaximumBlockBytes {
                actual: size,
                limit: self.block_bytes_limit,
            }
            .into())
        }
    }
```

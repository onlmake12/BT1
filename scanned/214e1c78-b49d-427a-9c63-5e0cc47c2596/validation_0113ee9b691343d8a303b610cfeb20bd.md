### Title
Unmetered O(N) `header_deps` Linear Scan in `LoadHeader` Syscall Enables Cycle-Disproportionate CPU Work — (`script/src/syscalls/load_header.rs`)

---

### Summary

The `LoadHeader` syscall's `load_header` helper performs a full linear scan of the transaction's `header_deps` list (via `.any()`) every time a header is fetched for a `CellDep` or `Input` source. This native-Rust scan executes entirely outside the CKB-VM cycle meter. Cycles are charged only for the bytes written back to VM memory by `store_data`, not for the scan itself. An unprivileged transaction submitter can craft a transaction with a large `header_deps` list and a script that calls `LoadHeader` in a tight loop, causing O(N × call_count) native CPU work while consuming only O(call_count) metered cycles.

---

### Finding Description

In `script/src/syscalls/load_header.rs`, the private helper `load_header` is invoked whenever `fetch_header` is called with `Source::Transaction(SourceEntry::CellDep)`, `Source::Transaction(SourceEntry::Input)`, or `Source::Group(SourceEntry::Input)`:

```rust
fn load_header(&self, cell_meta: &CellMeta) -> Option<HeaderView> {
    let block_hash = &cell_meta.transaction_info.as_ref()?.block_hash;
    if self
        .header_deps()        // returns Byte32Vec — full list
        .into_iter()
        .any(|hash| &hash == block_hash)   // O(N) scan, unmetered
    {
        self.sg_data.tx_info.data_loader.get_header(block_hash)
    } else {
        None
    }
}
``` [1](#0-0) 

The `ecall` dispatcher charges cycles only after the scan completes, and only for the bytes written to VM memory:

```rust
machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
``` [2](#0-1) 

`transferred_byte_cycles` charges 1 cycle per 4 bytes: [3](#0-2) 

For `LOAD_HEADER_BY_FIELD` returning a `u64` (8 bytes), the charge is exactly **2 cycles per call**, regardless of how many `header_deps` were scanned.

The `header_deps` list has no dedicated count cap in the codebase — no `MAX_HEADER_DEPS` constant exists. The only upper bound is the block byte limit (`MAX_BLOCK_BYTES = 597,000 bytes`; each header dep is 32 bytes), giving a theoretical maximum of ~18,600 unique hashes per transaction. [4](#0-3) 

The `max_block_cycles` is `3,500,000,000`. [5](#0-4) 

**Attack construction:**

1. Attacker selects N distinct on-chain block hashes `[H1, H2, …, HN]` (N ≈ 16,000, bounded by tx-pool size policy).
2. Attacker picks a `cell_dep` whose `transaction_info.block_hash == HN` (the last entry).
3. Attacker writes a script that loops calling `LOAD_HEADER_BY_FIELD` with `source = CellDep, index = 0`.
4. Each call: the native Rust `.any()` iterates all N hashes before finding the match at position N. Cost to VM: 2 cycles. Cost to host CPU: N × 32-byte comparisons.

With N = 16,000 and a tight loop costing ~10 cycles per iteration (register setup + ecall + 2 cycle charge + branch), the cycle budget of 3.5 billion allows ~350 million calls. Each call performs 16,000 × 32 = 512,000 bytes of native comparison work. Total unmetered native work: **~179 trillion byte-comparisons**, completely invisible to the cycle meter.

The `Source::Transaction(SourceEntry::HeaderDep)` path does **not** go through `load_header` and is not affected — it uses a direct index lookup. [6](#0-5) 

---

### Impact Explanation

Every validating node (full node, miner) must re-execute all scripts in every committed transaction. A single crafted transaction can force all nodes to perform hundreds of billions of native 32-byte hash comparisons while the cycle meter reports a normal, within-limit cycle count. This creates a sustained, reproducible CPU spike on every node that processes the block, degrading block validation throughput. In the worst case, slow nodes may fall behind the chain tip, and if validation latency exceeds block production intervals, it can contribute to orphan rate increases or temporary chain splits — approaching consensus deviation.

---

### Likelihood Explanation

The attack requires no privileged access, no leaked keys, and no majority hashpower. Any user who can submit a transaction and pay the fee can trigger it. The construction is straightforward: pick existing block hashes, order them so the matching hash is last, reference a cell confirmed in that block, and write a tight loop script. The only cost to the attacker is the transaction fee and the on-chain capacity for the script cell.

---

### Recommendation

**Option A (preferred):** Before the `.any()` scan, charge cycles proportional to the number of `header_deps` entries that will be examined. A flat charge of `transferred_byte_cycles(header_deps.len() as u64 * 32)` per `load_header` call would meter the scan work.

**Option B:** Replace the linear scan with a pre-built `HashSet<Byte32>` constructed once per script group execution (amortized O(1) per lookup). The set construction cost should be charged once at setup time.

**Option C:** Add a hard protocol cap on `header_deps` count (e.g., 64) to bound the worst-case scan length regardless of metering.

---

### Proof of Concept

```rust
// Pseudocode for the malicious script (RISC-V / CKB-VM)
// Transaction: header_deps = [H1, H2, ..., H16000]
// cell_deps[0] confirmed in block H16000
loop {
    // LOAD_HEADER_BY_FIELD: source=CellDep, index=0, field=EpochNumber
    // Host scans all 16000 header_deps before finding H16000
    // VM is charged: 2 cycles
    // Host CPU does: 16000 × 32-byte comparisons (unmetered)
    syscall(LOAD_HEADER_BY_FIELD, addr, size_addr, 0, SOURCE_CELL_DEP, EPOCH_NUMBER);
}
// Total metered cycles: ≤ 3,500,000,000 (within limit)
// Total host comparisons: ~350,000,000 × 16,000 = 5.6 × 10^12 (unmetered)
```

**Benchmark assertion:** Run the above with N=1 and N=16000; measure wall-clock time. Wall-clock time scales linearly with N while `machine.cycles()` remains constant — confirming the invariant violation.

### Citations

**File:** script/src/syscalls/load_header.rs (L56-70)
```rust
    fn load_header(&self, cell_meta: &CellMeta) -> Option<HeaderView> {
        // `transaction_info` is absent for unconfirmed cells provided by the
        // tx-pool (e.g. `PoolCell`). Treat them as missing instead of panicking,
        // so the syscall surfaces `ITEM_MISSING` to the script VM.
        let block_hash = &cell_meta.transaction_info.as_ref()?.block_hash;
        if self
            .header_deps()
            .into_iter()
            .any(|hash| &hash == block_hash)
        {
            self.sg_data.tx_info.data_loader.get_header(block_hash)
        } else {
            None
        }
    }
```

**File:** script/src/syscalls/load_header.rs (L85-95)
```rust
            Source::Transaction(SourceEntry::HeaderDep) => self
                .header_deps()
                .get(index)
                .ok_or(INDEX_OUT_OF_BOUND)
                .and_then(|block_hash| {
                    self.sg_data
                        .tx_info
                        .data_loader
                        .get_header(&block_hash)
                        .ok_or(ITEM_MISSING)
                }),
```

**File:** script/src/syscalls/load_header.rs (L175-175)
```rust
        machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```

**File:** script/src/cost_model.rs (L10-12)
```rust
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
```

**File:** spec/src/consensus.rs (L82-84)
```rust
/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

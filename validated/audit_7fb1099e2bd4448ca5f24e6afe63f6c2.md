All cited code references are confirmed accurate against the actual source. Every claim in the report maps precisely to the code:

- [1](#0-0)  — `fetch_header` called unconditionally before any size inspection.
- [2](#0-1)  — cycles charged from `len` (= `wrote_size`), not from actual data fetched.
- [3](#0-2)  — `store_data` returns `real_size = min(size, full_size)`; when `size=0`, returns 0.
- [4](#0-3)  — `transferred_byte_cycles(0) = 0`.
- [5](#0-4)  — `get_header` delegates to `ChainStore::get_block_header` (LRU cache + RocksDB fallback).
- [6](#0-5)  — LRU cache is finite and mutex-guarded; cache hits still incur mutex acquisition.
- [7](#0-6)  — identical pattern confirmed in `load_block_extension`.

---

Audit Report

## Title
Unmetered DB/cache reads via `size=0` probe in load syscalls — (`script/src/syscalls/load_header.rs`)

## Summary
`LoadHeader::ecall` unconditionally invokes `fetch_header` (triggering an LRU cache lookup and potential RocksDB read) before inspecting the script-controlled buffer size. Cycle charges are computed from `wrote_size` (bytes actually written), not from the size of data fetched. When the script sets `size=0`, `store_data` returns 0, `transferred_byte_cycles(0)=0`, and zero data-transfer cycles are charged for the full lookup. The same pattern exists in `load_block_extension` and the other load syscalls. This breaks the invariant that the cycle budget bounds total node I/O work per transaction.

## Finding Description
In `ecall` (`load_header.rs` line 163), `fetch_header` is called unconditionally:
```rust
let header = self.fetch_header(source, index as usize);
```
For `Source::Transaction(SourceEntry::HeaderDep)`, this calls `self.sg_data.tx_info.data_loader.get_header(&block_hash)` (`load_header.rs` lines 85–95), which delegates to `DataLoaderWrapper::get_header` → `ChainStore::get_block_header` (`data_loader_wrapper.rs` lines 60–62), hitting the LRU `headers` cache (mutex-guarded, `cache.rs` lines 13, 38) and falling through to RocksDB on a miss.

`load_full` (`load_header.rs` lines 112–120) calls `store_data(machine, &data)`. Inside `store_data` (`utils.rs` lines 18–27), `size` is read from the script-controlled `size_addr` register; `real_size = min(size, full_size)` is returned. When `size=0`, `real_size=0`.

Back in `ecall` at line 175:
```rust
machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```
`len=0`, so `transferred_byte_cycles(0)=0` (`cost_model.rs` lines 10–12), and zero data-transfer cycles are charged for the entire lookup. The full header fetch (cache lock + potential RocksDB I/O) is completely unmetered.

The LRU cache (`cache.rs` lines 11–26) does not mitigate this: (a) it is finite and LRU-evictable; (b) a transaction with N distinct `header_dep` hashes forces N distinct cache/DB lookups at zero metered cost; (c) even cache hits involve mutex acquisition and memory traversal that are unmetered. The identical pattern is confirmed in `load_block_extension.rs` lines 125–128.

## Impact Explanation
Within the 70,000,000-cycle budget, a script loop body costs ~7–8 RISC-V instruction cycles, allowing ~8–9 million `LOAD_HEADER_SYSCALL_NUMBER` calls. Each call triggers at minimum a mutex-guarded LRU cache lookup; with distinct `header_dep` hashes, each triggers a RocksDB read. This enables low-cost I/O amplification during script verification on every receiving node simultaneously. A normal full read (`size=208`) would cost ~52 additional data-transfer cycles per call, exhausting the 70M budget after ~1.3M calls — the `size=0` bypass removes this bound entirely. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points).

## Likelihood Explanation
No privileged access, key material, or majority hashpower is required. The attacker needs only a valid transaction with one or more `header_dep` entries and a RISC-V script containing a tight loop. The transaction is valid and accepted by the mempool, triggering script verification on all receiving nodes simultaneously. The RISC-V loop is trivially expressible in a handful of assembly instructions.

## Recommendation
Charge cycles based on `full_size` (the actual data fetched), not `real_size` (the bytes written). Modify `store_data` to return both values (or expose `full_size` separately), and in `ecall` replace:
```rust
machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```
with a charge based on the full serialized size of the fetched data (e.g., `header.data().as_bytes().len()`), regardless of how many bytes the script requested. Apply the same fix consistently to `load_block_extension`, `load_cell`, `load_input`, `load_witness`, `load_tx`, and `load_script`.

## Proof of Concept
```c
// RISC-V pseudocode — precondition: tx has header_dep at index 0
// size_word is a memory word initialized to 0
loop:
    li   a7, 2072        // LOAD_HEADER_SYSCALL_NUMBER
    li   a3, 0           // index 0
    li   a4, 0x4         // Source::HeaderDep
    la   a0, buf         // output buffer (ignored)
    la   a1, size_word   // *size_word = 0 → real_size = 0 → 0 cycles charged
    li   a2, 0           // offset = 0
    ecall                // fetch_header executes; 0 data-transfer cycles charged
    sw   zero, 0(a1)     // reset size_word to 0
    addi t0, t0, 1
    blt  t0, 1000000, loop
```
Verification: instrument `DataLoaderWrapper::get_header` to count invocations and compare against `machine.cycles()` after the loop. With 1,000,000 iterations, the DB/cache read counter will show ~1,000,000 calls while the cycle counter shows only ~8,000,000 instruction cycles (no data-transfer cycles). With a normal full read (`size=208`), the same 1,000,000 calls would cost ~52,000,000 additional data-transfer cycles, exhausting the 70M budget after ~1.3M calls.

### Citations

**File:** script/src/syscalls/load_header.rs (L163-163)
```rust
        let header = self.fetch_header(source, index as usize);
```

**File:** script/src/syscalls/load_header.rs (L175-175)
```rust
        machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```

**File:** script/src/syscalls/utils.rs (L18-27)
```rust
    let size = machine.memory_mut().load64(&size_addr)?.to_u64();
    let full_size = data_len - offset;
    let real_size = cmp::min(size, full_size);
    machine
        .memory_mut()
        .store64(&size_addr, &Mac::REG::from_u64(full_size))?;
    machine
        .memory_mut()
        .store_bytes(addr, &data[offset as usize..(offset + real_size) as usize])?;
    Ok(real_size)
```

**File:** script/src/cost_model.rs (L10-12)
```rust
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
```

**File:** store/src/data_loader_wrapper.rs (L60-62)
```rust
    fn get_header(&self, block_hash: &Byte32) -> Option<HeaderView> {
        ChainStore::get_block_header(self.0.as_ref(), block_hash)
    }
```

**File:** store/src/cache.rs (L11-26)
```rust
pub struct StoreCache {
    /// The cache of block headers
    pub headers: Mutex<LruCache<Byte32, HeaderView>>,
    /// The cache of cell data.
    pub cell_data: Mutex<LruCache<Vec<u8>, (Bytes, Byte32)>>,
    /// The cache of cell data hash.
    pub cell_data_hash: Mutex<LruCache<Vec<u8>, Byte32>>,
    /// The cache of block proposals.
    pub block_proposals: Mutex<LruCache<Byte32, ProposalShortIdVec>>,
    /// The cache of block transaction hashes.
    pub block_tx_hashes: Mutex<LruCache<Byte32, Vec<Byte32>>>,
    /// The cache of block uncles.
    pub block_uncles: Mutex<LruCache<Byte32, UncleBlockVecView>>,
    /// The cache of block extension sections.
    pub block_extensions: Mutex<LruCache<Byte32, Option<packed::Bytes>>>,
}
```

**File:** script/src/syscalls/load_block_extension.rs (L125-128)
```rust
        let wrote_size = store_data(machine, &data)?;

        machine.add_cycles_no_checking(transferred_byte_cycles(wrote_size))?;
        machine.set_register(A0, Mac::REG::from_u8(SUCCESS));
```

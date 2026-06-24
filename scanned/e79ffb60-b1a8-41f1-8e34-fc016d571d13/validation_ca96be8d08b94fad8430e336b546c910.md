Audit Report

## Title
Unmetered DB/cache reads via `size=0` probe in load syscalls — (`script/src/syscalls/load_header.rs`)

## Summary
`LoadHeader::ecall` performs a full header lookup via `fetch_header` (which calls `data_loader.get_header`) unconditionally before inspecting the requested buffer size. Cycle charges are computed from `store_data`'s return value (`real_size` = bytes written), not from the actual data fetched. When the script sets the size field to 0, `store_data` returns 0, `transferred_byte_cycles(0) = 0`, and `add_cycles_no_checking(0)` charges nothing for the lookup. The same pattern exists in `load_block_extension`, `load_cell`, `load_input`, `load_witness`, `load_tx`, and `load_script`. This breaks the invariant that the cycle budget bounds total node work per transaction.

## Finding Description

In `ecall` (`load_header.rs` lines 153–178), `fetch_header` is called unconditionally at line 163 before any size inspection:

```rust
let header = self.fetch_header(source, index as usize);
```

`fetch_header` for `Source::Transaction(SourceEntry::HeaderDep)` calls `self.sg_data.tx_info.data_loader.get_header(&block_hash)` directly. In production, `DataLoaderWrapper::get_header` delegates to `ChainStore::get_block_header`, which checks the LRU header cache (`store/src/cache.rs` line 13) and falls through to RocksDB on a miss.

`load_full` (`load_header.rs` lines 112–120) calls `store_data(machine, &data)`, which reads `size` from the script-controlled `size_addr` register, computes `real_size = min(size, full_size)`, and returns `real_size` (`utils.rs` lines 18–27). When `size = 0`, `real_size = 0`.

Back in `ecall` at line 175:
```rust
machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```
`len` is `wrote_size = 0`, so `transferred_byte_cycles(0) = 0` (`cost_model.rs` lines 10–12), and zero cycles are charged for the entire lookup.

The LRU cache (`store/src/cache.rs` lines 11–26) partially mitigates repeated reads of the same header hash, but: (a) the cache is finite and LRU-evictable; (b) a transaction with N distinct `header_dep` hashes forces N distinct cache/DB lookups, all at zero metered cost; (c) even cache hits involve mutex acquisition and memory traversal that are unmetered.

## Impact Explanation

A script can loop calling `LOAD_HEADER_SYSCALL_NUMBER` (2072) with `*size_addr = 0`. Each iteration costs only the RISC-V instruction cycles for the loop body (~7–8 cycles). Within the 70,000,000-cycle budget, a script can issue ~8–9 million such calls. With N distinct `header_dep` entries, each unique hash forces a cache/DB read at zero data-transfer cycle cost. This enables low-cost I/O amplification during script verification, allowing an attacker to submit transactions that consume disproportionate node CPU and I/O resources relative to their cycle cost.

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points). The transaction can be submitted via standard RPC (`send_transaction`), triggering script verification in the tx-pool on every receiving node.

## Likelihood Explanation

No privileged access, key material, or majority hashpower is required. The attacker needs only a valid transaction with one or more `header_dep` entries. The RISC-V loop is trivially expressible in a few assembly instructions. The transaction is valid and will be accepted by the mempool, triggering verification on all receiving nodes simultaneously.

## Recommendation

Charge cycles based on `full_size` (the actual data fetched), not `real_size` (the bytes written). In `store_data`, return both values, or expose `full_size` separately. In `ecall`, replace:

```rust
machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```

with a charge based on the full serialized size of the header (i.e., `header.data().as_bytes().len()`), regardless of how many bytes the script requested. The same fix must be applied consistently to `load_block_extension`, `load_cell`, `load_input`, `load_witness`, `load_tx`, and `load_script`.

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

Verification: instrument `DataLoaderWrapper::get_header` to count invocations and compare against `machine.cycles()` after the loop. With 1,000,000 iterations, the DB/cache read counter will show ~1,000,000 calls while the cycle counter shows only ~8,000,000 instruction cycles (no data-transfer cycles). With a normal full read (size = 208), the same 1,000,000 calls would cost ~52,000,000 additional data-transfer cycles, exhausting the 70M budget after ~1.3M calls. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** script/src/syscalls/load_header.rs (L112-120)
```rust
    fn load_full<Mac: SupportMachine>(
        &self,
        machine: &mut Mac,
        header: &HeaderView,
    ) -> Result<(u8, u64), VMError> {
        let data = header.data().as_bytes();
        let wrote_size = store_data(machine, &data)?;
        Ok((SUCCESS, wrote_size))
    }
```

**File:** script/src/syscalls/load_header.rs (L163-175)
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

**File:** store/src/data_loader_wrapper.rs (L56-62)
```rust
impl<T> HeaderProvider for DataLoaderWrapper<T>
where
    T: ChainStore,
{
    fn get_header(&self, block_hash: &Byte32) -> Option<HeaderView> {
        ChainStore::get_block_header(self.0.as_ref(), block_hash)
    }
```

**File:** script/src/syscalls/load_block_extension.rs (L125-128)
```rust
        let wrote_size = store_data(machine, &data)?;

        machine.add_cycles_no_checking(transferred_byte_cycles(wrote_size))?;
        machine.set_register(A0, Mac::REG::from_u8(SUCCESS));
```

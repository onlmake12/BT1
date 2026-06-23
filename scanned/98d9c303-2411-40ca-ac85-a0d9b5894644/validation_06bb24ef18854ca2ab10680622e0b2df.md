### Title
Uncharged O(N) `header_deps` Linear Scan in `LoadHeader::load_header` Enables Cycle-Budget Bypass DoS — (`script/src/syscalls/load_header.rs`)

---

### Summary

The `LoadHeader` syscall performs an O(N) linear membership scan over all `header_deps` on every invocation for `Source::Input` and `Source::CellDep` paths, but charges cycles only for the bytes written to VM memory. A script calling `ckb_load_header` in a tight loop with a transaction carrying many `header_deps` can consume arbitrarily more wall-clock CPU time than the cycle budget implies, breaking the cycle model's role as a DoS bound.

---

### Finding Description

In `script/src/syscalls/load_header.rs`, `LoadHeader::load_header` (called by `fetch_header` for `Source::Transaction(Input)`, `Source::Transaction(CellDep)`, and `Source::Group(Input)`) performs a full linear scan over the transaction's `header_deps` list to verify membership before fetching the header: [1](#0-0) 

The scan at lines 61–64 iterates over every element of `header_deps()` (a `Byte32Vec` molecule type, re-materialized on every call) and compares 32-byte hashes. The code comment at line 32 explicitly acknowledges this design: *"This can only be used for liner search."* [2](#0-1) 

After the scan, the only cycle charge is: [3](#0-2) 

This charges `transferred_byte_cycles(len)` — proportional to the bytes written to VM memory (the header data, ~208 bytes for `LOAD_HEADER`, or 8 bytes for `LOAD_HEADER_BY_FIELD`). The O(N) scan work is **never charged**. [4](#0-3) 

The consensus parameters establish the bounds: [5](#0-4) 

- `MAX_BLOCK_BYTES = 597,000 bytes` → max header_deps per transaction ≈ `597,000 / 32 ≈ 18,656` (each is a 32-byte hash)
- `MAX_BLOCK_CYCLES = 3,500,000,000 cycles`

Using `LOAD_HEADER_BY_FIELD`, the minimum cycle cost per invocation is `transferred_byte_cycles(8) = 2 cycles` plus RISC-V instruction overhead (~10–20 cycles total). At ~20 cycles/call with 3.5B max cycles: **~175 million calls**, each performing an 18,000-element × 32-byte scan = **~100 terabytes of comparison work** per transaction verification.

The `DuplicateDepsVerifier` prevents duplicate header_deps but does not limit their count: [6](#0-5) 

All `header_deps` must reference valid canonical-chain blocks (enforced by `HeaderChecker::check_valid`), but on mainnet with millions of blocks this is trivially satisfiable: [7](#0-6) 

---

### Impact Explanation

During script verification (both tx-pool admission and block verification), a node executes the CKB-VM for each script group. A single crafted transaction can cause the verifier to spend orders of magnitude more CPU time than the cycle budget implies. This breaks the fundamental invariant that `max_block_cycles` bounds verification wall time, enabling effective denial of service against any node that processes the transaction or the block containing it.

---

### Likelihood Explanation

The attack is fully unprivileged: any user can submit a transaction via `send_transaction` RPC or P2P relay. The attacker only needs to:
1. Reference N distinct canonical-chain block hashes as `header_deps` (trivially available on mainnet)
2. Deploy a minimal script that calls `ckb_load_header` (or `ckb_load_header_by_field`) in a tight loop against a fixed input index
3. Submit the transaction

No special keys, hashpower, or privileged access are required.

---

### Recommendation

Charge cycles proportional to the number of `header_deps` scanned on each `load_header` invocation. A fixed base cost of `N` cycles (where N = `header_deps().len()`) added before or during the scan would align the cycle model with actual work. Alternatively, pre-build a `HashSet<Byte32>` from `header_deps` once per script group execution and use O(1) lookups, eliminating the scan entirely.

---

### Proof of Concept

```
Transaction layout:
  header_deps: [h1, h2, ..., h_N]   (N = ~10,000 distinct canonical block hashes)
  inputs: [one live UTXO]
  cell_deps: [script_cell]
  witnesses: [...]

Script (RISC-V, ~50 bytes):
  loop:
    li a3, 0                  // index = 0 (first input)
    li a4, 1                  // source = CKB_SOURCE_INPUT
    li a5, 0                  // field = EpochNumber
    li a7, 0x6                // LOAD_HEADER_BY_FIELD syscall
    ecall                     // triggers O(N) scan, charges ~20 cycles
    j loop                    // repeat until cycle limit

Measurement:
  - Cycles consumed: 3,500,000,000 (max_block_cycles)
  - Syscall invocations: ~175,000,000
  - Scan iterations per call: N = 10,000
  - Total hash comparisons: 1.75 × 10^12
  - Expected wall time (at 10^10 comparisons/sec): ~175 seconds
  - Expected wall time per cycle model: <1 second
```

The ratio of actual wall time to cycle-model-predicted time grows linearly with N, confirming the O(N) amplification factor is fully attacker-controlled within the block size limit.

### Citations

**File:** script/src/syscalls/load_header.rs (L32-35)
```rust
    // This can only be used for liner search
    // header_deps: Byte32Vec,
    // resolved_inputs: &'a [CellMeta],
    // resolved_cell_deps: &'a [CellMeta],
```

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

**File:** script/src/syscalls/load_header.rs (L175-175)
```rust
        machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```

**File:** script/src/cost_model.rs (L7-12)
```rust
pub const BYTES_PER_CYCLE: u64 = 4;

/// Calculates how many cycles spent to load the specified number of bytes.
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
```

**File:** spec/src/consensus.rs (L70-84)
```rust
pub const TWO_IN_TWO_OUT_CYCLES: Cycle = 3_500_000;
/// bytes of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_BYTES: u64 = 597;
/// count of two-in-two-out txs a block should capable to package.
const TWO_IN_TWO_OUT_COUNT: u64 = 1_000;
pub(crate) const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 4 hours, unit: second
const MILLISECONDS_IN_A_SECOND: u64 = 1000;
const MAX_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MIN_BLOCK_INTERVAL; // 1800
const MIN_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MAX_BLOCK_INTERVAL; // 300
pub(crate) const DEFAULT_PRIMARY_EPOCH_REWARD_HALVING_INTERVAL: EpochNumber =
    4 * 365 * 24 * 60 * 60 / DEFAULT_EPOCH_DURATION_TARGET; // every 4 years

/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** verification/src/transaction_verifier.rs (L437-458)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let transaction = self.transaction;
        let mut seen_cells = HashSet::with_capacity(self.transaction.cell_deps().len());
        let mut seen_headers = HashSet::with_capacity(self.transaction.header_deps().len());

        if let Some(dep) = transaction
            .cell_deps_iter()
            .find_map(|dep| seen_cells.replace(dep))
        {
            return Err(TransactionError::DuplicateCellDeps {
                out_point: dep.out_point(),
            }
            .into());
        }
        if let Some(hash) = transaction
            .header_deps_iter()
            .find_map(|hash| seen_headers.replace(hash))
        {
            return Err(TransactionError::DuplicateHeaderDeps { hash }.into());
        }
        Ok(())
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L74-83)
```rust
impl<CS: ChainStore> HeaderChecker for VerifyContext<CS> {
    fn check_valid(&self, block_hash: &Byte32) -> Result<(), OutPointError> {
        if !self.store.is_main_chain(block_hash) {
            return Err(OutPointError::InvalidHeader(block_hash.clone()));
        }
        self.store
            .get_block_header(block_hash)
            .ok_or_else(|| OutPointError::InvalidHeader(block_hash.clone()))?;
        Ok(())
    }
```

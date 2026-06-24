Looking at the actual code to validate each claim before rendering a verdict.

Audit Report

## Title
Uncharged O(N) `header_deps` Linear Scan in `LoadHeader::load_header` Enables Cycle-Budget Bypass DoS — (`script/src/syscalls/load_header.rs`)

## Summary
`LoadHeader::load_header` performs a full linear scan over all `header_deps` on every invocation for `Source::Input`, `Source::CellDep`, and `Source::Group(Input)` paths, but charges cycles only for bytes written to VM memory. A script calling `ckb_load_header_by_field` in a tight loop against a transaction carrying thousands of `header_deps` can consume orders of magnitude more CPU time than the cycle budget implies, breaking the invariant that `max_block_cycles` bounds verification wall time and enabling effective network congestion.

## Finding Description
In `script/src/syscalls/load_header.rs`, `load_header` (lines 56–70) performs a full linear scan over the transaction's `header_deps` list on every invocation:

```rust
// lines 61–64
if self
    .header_deps()
    .into_iter()
    .any(|hash| &hash == block_hash)
```

`header_deps()` (line 42–44) re-materializes a `Byte32Vec` from the molecule-encoded transaction on every call. The scan iterates over every 32-byte hash in the list. After the scan, the only cycle charge is at line 175:

```rust
machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```

This charges cycles proportional to bytes written to VM memory — 2 cycles for `LOAD_HEADER_BY_FIELD` (8 bytes / 4 bytes-per-cycle). The O(N) scan work is never charged.

**Exploit path:**
1. Attacker constructs a transaction with N distinct valid canonical-chain block hashes as `header_deps` (e.g., N = 10,000; 320,000 bytes of hashes, within `MAX_BLOCK_BYTES = 597,000`).
2. Attacker deploys a minimal RISC-V script that calls `ckb_load_header_by_field` in a tight loop against a fixed input index.
3. Each syscall invocation triggers the O(N) scan (uncharged) and charges only ~2 cycles.
4. With `MAX_BLOCK_CYCLES = 3,500,000,000` and ~20 cycles/call overhead: ~175 million invocations, each scanning 10,000 × 32 bytes = ~55 terabytes of comparison work per transaction verification.

**Existing guards are insufficient:**
- `DuplicateDepsVerifier` (lines 437–458 of `transaction_verifier.rs`) only rejects duplicate `header_deps`; it imposes no count limit.
- `HeaderChecker::check_valid` (lines 74–83 of `contextual_block_verifier.rs`) only validates that each hash is a canonical-chain block — trivially satisfiable on mainnet with millions of blocks.
- No per-transaction `header_deps` count cap exists anywhere in the verifier pipeline.

## Impact Explanation
This breaks the fundamental invariant that `max_block_cycles` bounds script verification wall time. A single crafted transaction causes every node that processes it (during tx-pool admission or block verification) to spend orders of magnitude more CPU than the cycle budget implies. The amplification factor is fully attacker-controlled and grows linearly with N (bounded only by block size). This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The attack is fully unprivileged. The attacker needs only: a live UTXO, a deployed script cell, and N distinct canonical block hashes (freely available via any CKB node's RPC). No special keys, hashpower, or privileged access are required. The transaction can be submitted via `send_transaction` RPC or P2P relay. The attack is repeatable and cheap — transaction fees are bounded by the small cycle count charged, not by actual CPU work performed.

## Recommendation
Charge cycles proportional to the number of `header_deps` scanned on each `load_header` invocation. Before or during the scan, add a base cost of `header_deps().len()` cycles (one cycle per 32-byte hash compared). Alternatively, pre-build a `HashSet<Byte32>` from `header_deps` once per script group execution (e.g., in `LoadHeader::new` or at script group setup time) and use O(1) lookups, eliminating the repeated scan entirely. The latter approach also eliminates the repeated molecule deserialization overhead from re-materializing `Byte32Vec` on every call.

## Proof of Concept
```
Transaction layout:
  header_deps: [h1, h2, ..., h_N]   (N = 10,000 distinct canonical block hashes)
  inputs: [one live UTXO]
  cell_deps: [script_cell]
  witnesses: [...]

Script (RISC-V):
  loop:
    li a3, 0          // index = 0 (first input)
    li a4, 1          // source = CKB_SOURCE_INPUT
    li a5, 0          // field = EpochNumber
    li a7, 0x6        // LOAD_HEADER_BY_FIELD syscall number
    ecall             // triggers O(N=10,000) scan, charges ~2 cycles
    j loop            // repeat until cycle limit

Expected result:
  - Cycles charged: 3,500,000,000 (hits max_block_cycles)
  - Syscall invocations: ~175,000,000
  - Scan iterations per call: 10,000
  - Total hash comparisons: ~1.75 × 10^12
  - Actual CPU work: ~175 seconds at 10^10 comparisons/sec
  - Cycle-model-predicted time: <1 second
```

The ratio of actual wall time to cycle-model-predicted time grows linearly with N, confirming the O(N) amplification is fully attacker-controlled within the block size limit. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** spec/src/consensus.rs (L83-84)
```rust
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

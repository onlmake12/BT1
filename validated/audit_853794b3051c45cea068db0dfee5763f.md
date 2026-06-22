### Title
Hardcoded Block Version Literal in Sync Layer Diverges from Consensus-Defined `block_version` — (`File: sync/src/synchronizer/headers_process.rs`)

---

### Summary

The `HeaderAcceptor::version_check()` in the sync layer hardcodes the expected block version as the literal `0` instead of reading the authoritative value from the `Consensus` object. Meanwhile, the block assembler correctly reads `consensus.block_version()` when constructing new blocks. This structural mismatch means that if `block_version` is ever updated in the consensus (e.g., via a hardfork), the sync layer will permanently reject all valid blocks from peers, marking them `BLOCK_INVALID`, while the block assembler continues producing blocks with the new version.

---

### Finding Description

In `util/types/src/constants.rs`, the canonical protocol constants are defined:

```rust
pub const TX_VERSION: Version = 0;
pub const BLOCK_VERSION: Version = 0;
``` [1](#0-0) 

The `Consensus` struct carries a `block_version` field as the runtime-authoritative value, and exposes it via `consensus.block_version()`: [2](#0-1) [3](#0-2) 

The block assembler correctly reads this consensus value when constructing block templates:

```rust
let version = consensus.block_version();
``` [4](#0-3) 

The transaction verifier also correctly reads from consensus:

```rust
version: VersionVerifier::new(tx, consensus.tx_version()),
``` [5](#0-4) 

However, `HeaderAcceptor::version_check()` in the sync layer hardcodes the literal `0` instead of reading from the consensus object:

```rust
pub fn version_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
    if self.header.version() != 0 {   // ← hardcoded literal, not consensus.block_version()
        state.invalid(Some(ValidationError::Version));
        Err(())
    } else {
        Ok(())
    }
}
``` [6](#0-5) 

This check is invoked in `HeaderAcceptor::accept()` and, on failure, marks the block hash as `BLOCK_INVALID` in shared state: [7](#0-6) 

The `HeaderAcceptor` struct holds a `verifier: HeaderVerifier<'a, DL>` which itself carries `consensus: &'a Consensus`, so the consensus value is structurally accessible but is not used in `version_check()`. [8](#0-7) 

Notably, the canonical `HeaderVerifier` used for full block verification does **not** include a block version check at all — it only checks PoW, number, epoch, and timestamp: [9](#0-8) 

This means the only enforced block version check in the sync path is the hardcoded `!= 0` literal in `version_check()`, which is inconsistent with the consensus-driven approach used everywhere else.

---

### Impact Explanation

**High.** If `block_version` is incremented in the consensus (e.g., to `1` via a hardfork), the block assembler will produce blocks with `version = 1`, and those blocks will be relayed by peers. Any node running the current code will enter `version_check()`, see `1 != 0`, mark the block hash as `BLOCK_INVALID` in shared state, and permanently refuse to sync that header or any descendant. The node becomes unable to follow the canonical chain. Because `BLOCK_INVALID` is cached in shared state, the damage persists for the lifetime of the process.

---

### Likelihood Explanation

**Low-to-Medium.** Currently `BLOCK_VERSION = 0` and `consensus.block_version() = 0`, so there is no active mismatch. However, CKB's hardfork mechanism (`HardForks`, versionbits) is explicitly designed to allow future protocol upgrades, and `block_version` is a named, mutable field in `Consensus`. The structural divergence between the sync layer's hardcoded literal and the consensus-driven value used by the block assembler is a latent defect that will manifest the moment any upgrade increments `block_version`.

---

### Recommendation

Replace the hardcoded literal `0` in `HeaderAcceptor::version_check()` with the consensus-authoritative value. Since `HeaderAcceptor` already holds `active_chain` (which provides access to the shared consensus), the fix is:

```rust
pub fn version_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
    let expected = self.active_chain.sync_shared().shared().consensus().block_version();
    if self.header.version() != expected {
        state.invalid(Some(ValidationError::Version));
        Err(())
    } else {
        Ok(())
    }
}
```

This aligns the sync layer with the block assembler's approach and ensures both components stay in sync with the consensus-defined `block_version` across any future hardfork.

---

### Proof of Concept

1. Modify `spec/src/consensus.rs` to set `block_version` to `1` (simulating a hardfork increment).
2. Start a CKB node. The block assembler will produce blocks with `version = 1` (via `consensus.block_version()`).
3. A peer relays a header with `version = 1` to the node.
4. `HeaderAcceptor::version_check()` evaluates `1 != 0` → `true`, calls `state.invalid(...)`, and inserts `BLOCK_INVALID` for that hash.
5. The node permanently refuses to accept the header or any descendant, halting sync, while the block assembler continues producing `version = 1` blocks — demonstrating the split between the two components.

### Citations

**File:** util/types/src/constants.rs (L6-8)
```rust
pub const TX_VERSION: Version = 0;
/// Current block version.
pub const BLOCK_VERSION: Version = 0;
```

**File:** spec/src/consensus.rs (L563-565)
```rust
    pub block_version: Version,
    /// The tx version number supported
    pub tx_version: Version,
```

**File:** spec/src/consensus.rs (L748-751)
```rust
    /// The current block version
    pub fn block_version(&self) -> Version {
        self.block_version
    }
```

**File:** tx-pool/src/block_assembler/mod.rs (L832-833)
```rust
        let version = consensus.block_version();
        let max_block_bytes = consensus.max_block_bytes();
```

**File:** verification/src/transaction_verifier.rs (L84-84)
```rust
            version: VersionVerifier::new(tx, consensus.tx_version()),
```

**File:** sync/src/synchronizer/headers_process.rs (L222-227)
```rust
pub struct HeaderAcceptor<'a, DL: HeaderFieldsProvider> {
    header: &'a core::HeaderView,
    active_chain: ActiveChain,
    peer: PeerIndex,
    verifier: HeaderVerifier<'a, DL>,
}
```

**File:** sync/src/synchronizer/headers_process.rs (L286-293)
```rust
    pub fn version_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
        if self.header.version() != 0 {
            state.invalid(Some(ValidationError::Version));
            Err(())
        } else {
            Ok(())
        }
    }
```

**File:** sync/src/synchronizer/headers_process.rs (L346-354)
```rust
        if self.version_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-version header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }
```

**File:** verification/src/header_verifier.rs (L32-50)
```rust
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
        let parent_fields = self
            .data_loader
            .get_header_fields(&header.parent_hash())
            .ok_or_else(|| UnknownParentError {
                parent_hash: header.parent_hash(),
            })?;
        NumberVerifier::new(parent_fields.number, header).verify()?;
        EpochVerifier::new(parent_fields.epoch, header).verify()?;
        TimestampVerifier::new(
            self.data_loader,
            header,
            self.consensus.median_time_block_count(),
        )
        .verify()?;
        Ok(())
    }
```

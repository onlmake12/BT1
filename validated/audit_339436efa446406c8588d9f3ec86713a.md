### Title
Non-Contextual `EpochVerifier` Accepts Arbitrary Epoch Lengths at Epoch Transitions, Enabling Sync Disruption via Crafted Headers — (File: `verification/src/header_verifier.rs`)

---

### Summary

The `EpochVerifier` used during P2P header sync (`HeadersProcess`) relies on `is_successor_of()` to validate epoch continuity. At epoch boundaries, `is_successor_of()` does **not** validate the epoch length of the new epoch. A malicious P2P peer can craft a chain of headers where each block claims to be in its own single-block epoch (length = 1), all with valid PoW. These headers pass non-contextual verification and are inserted into the sync header map, potentially causing the node to prefer the attacker's chain during IBD and wasting resources downloading blocks that will only fail later during contextual verification.

---

### Finding Description

**Root cause — `is_successor_of()` skips epoch length at epoch transitions:**

In `util/types/src/core/extras.rs`, `is_successor_of()` has two branches:

```rust
pub fn is_successor_of(self, predecessor: Self) -> bool {
    if predecessor.index() + 1 == predecessor.length() {
        // Last block of epoch → first block of next epoch
        self.number() == predecessor.number() + 1 && self.index() == 0
        // ← self.length() is NEVER checked here
    } else {
        self.number() == predecessor.number()
            && self.index() == predecessor.index() + 1
            && self.length() == predecessor.length()  // ← length IS checked within an epoch
    }
}
``` [1](#0-0) 

Within an epoch, `self.length()` is enforced. But at the epoch boundary (the `if` branch), only the epoch number increment and `index == 0` are checked. The epoch length of the new epoch is unconstrained.

**The non-contextual `EpochVerifier` in `header_verifier.rs` relies solely on this check:**

```rust
pub fn verify(&self) -> Result<(), Error> {
    if !self.header.epoch().is_well_formed() { ... }
    if !self.parent.is_genesis() && !self.header.epoch().is_successor_of(self.parent) {
        return Err(EpochError::NonContinuous { ... }.into());
    }
    Ok(())
}
``` [2](#0-1) 

`is_well_formed()` only requires `length > 0 && length > index`. So for the first block of a new epoch (index = 0), any length ≥ 1 is accepted.

**This verifier is invoked for every header received from a P2P peer during sync:**

```rust
for header in headers.iter().skip(1) {
    let verifier = HeaderVerifier::new(shared, consensus);
    let acceptor = HeaderAcceptor::new(header, self.peer, verifier, self.active_chain.clone());
    let result = acceptor.accept();
    ...
}
``` [3](#0-2) 

**The compact_target and epoch length are only validated contextually**, in `contextual_block_verifier.rs`, which computes the expected `EpochExt` from `consensus.next_epoch_ext()` and checks both fields:

```rust
if actual_epoch_with_fraction != epoch_with_fraction { ... }
if self.epoch.compact_target() != actual_compact_target { ... }
``` [4](#0-3) 

This contextual check only runs when a full block is processed by the chain service — **not** during the header-first sync phase.

**Accepted headers are inserted into the header map and influence peer selection:**

```rust
let mut header_view = HeaderIndexView::new(
    header.hash(), header.number(), header.epoch(), header.timestamp(),
    parent_hash,
    parent_header_index.total_difficulty() + header.difficulty(),
);
...
self.state.may_set_shared_best_header(header_view);
``` [5](#0-4) 

---

### Impact Explanation

A malicious peer sends a `SendHeaders` message containing a chain of headers where every block claims epoch length = 1 (each block is its own single-block epoch). Each header:

- Passes `is_well_formed()` (length=1 > 0, index=0 < length=1)
- Passes `is_successor_of()` at every step (each block is the "last" block of its epoch, so the next block only needs epoch number + 1 and index = 0)
- Passes the PoW check (the attacker mines valid nonces for the claimed compact_target)
- Passes number and timestamp checks

These headers are inserted into the sync header map. If the attacker's chain has a higher total_difficulty than the honest chain (requiring real PoW work), the node will:

1. Set the attacker's chain as `shared_best_header`
2. Request full blocks from the attacker
3. Fail contextual verification for every block (epoch mismatch)
4. Waste bandwidth and CPU during IBD

During IBD, the node may also disconnect from honest outbound peers that appear to have lower total_difficulty:

```rust
if self.active_chain.is_initial_block_download()
    && headers.len() != MAX_HEADERS_LEN
    && (!peer_flags.is_protect && !peer_flags.is_whitelist && peer_flags.is_outbound)
{
    // Disconnect an unprotected outbound peer
``` [6](#0-5) 

This can stall IBD by causing the node to disconnect from honest peers and loop on a fake chain.

---

### Likelihood Explanation

Any unprivileged P2P peer can send crafted `SendHeaders` messages. The attacker needs to perform real PoW work proportional to the claimed compact_target to make the fake chain competitive in total_difficulty. On mainnet this is expensive; on testnet or devnet it is cheap. The attack is reachable without any privileged access, leaked keys, or majority hashpower.

---

### Recommendation

In `EpochVerifier::verify()` in `verification/src/header_verifier.rs`, when the header is the first block of a new epoch, validate that the epoch length falls within the consensus-enforced bounds (`min_epoch_length` to `max_epoch_length`). Specifically, `is_successor_of()` should either be extended to enforce length bounds at epoch transitions, or the `EpochVerifier` should perform an additional range check on `self.header.epoch().length()` when `self.header.epoch().index() == 0`. This mirrors the contextual check in `contextual_block_verifier.rs` but at the cheaper, earlier header-sync stage, preventing malformed epoch-length headers from ever entering the header map.

---

### Proof of Concept

1. Connect to a CKB node as a P2P peer.
2. Construct a chain of headers starting from a known block, where each header has:
   - `epoch.number` = previous + 1
   - `epoch.index` = 0
   - `epoch.length` = 1 (minimum valid value)
   - A valid PoW nonce for the claimed `compact_target`
3. Send these headers via `SendHeaders` (up to `MAX_HEADERS_LEN` = 2000 per message).
4. Observe that `HeadersProcess::execute()` accepts all headers without error.
5. Observe that `shared_best_header` is updated to the attacker's chain tip if total_difficulty exceeds the honest chain.
6. Observe that the node begins requesting full blocks from the attacker, which all fail contextual verification with `EpochError::NumberMismatch`, stalling IBD.

### Citations

**File:** util/types/src/core/extras.rs (L510-518)
```rust
    pub fn is_successor_of(self, predecessor: Self) -> bool {
        if predecessor.index() + 1 == predecessor.length() {
            self.number() == predecessor.number() + 1 && self.index() == 0
        } else {
            self.number() == predecessor.number()
                && self.index() == predecessor.index() + 1
                && self.length() == predecessor.length()
        }
    }
```

**File:** verification/src/header_verifier.rs (L133-148)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if !self.header.epoch().is_well_formed() {
            return Err(EpochError::Malformed {
                value: self.header.epoch(),
            }
            .into());
        }
        if !self.parent.is_genesis() && !self.header.epoch().is_successor_of(self.parent) {
            return Err(EpochError::NonContinuous {
                current: self.header.epoch(),
                parent: self.parent,
            }
            .into());
        }
        Ok(())
    }
```

**File:** sync/src/synchronizer/headers_process.rs (L154-179)
```rust
        for header in headers.iter().skip(1) {
            let verifier = HeaderVerifier::new(shared, consensus);
            let acceptor =
                HeaderAcceptor::new(header, self.peer, verifier, self.active_chain.clone());
            let result = acceptor.accept();
            match result.state {
                ValidationState::Invalid => {
                    debug!(
                        "HeadersProcess accept result is invalid, error = {:?}, header = {:?}",
                        result.error, headers,
                    );
                    return StatusCode::HeadersIsInvalid
                        .with_context(format!("accept header {header:?}"));
                }
                ValidationState::TemporaryInvalid => {
                    debug!(
                        "HeadersProcess accept result is temporarily invalid, header = {:?}",
                        header
                    );
                    return Status::ok();
                }
                ValidationState::Valid => {
                    // Valid, do nothing
                }
            };
        }
```

**File:** sync/src/synchronizer/headers_process.rs (L201-216)
```rust
        if self.active_chain.is_initial_block_download()
            && headers.len() != MAX_HEADERS_LEN
            && (!peer_flags.is_protect && !peer_flags.is_whitelist && peer_flags.is_outbound)
        {
            debug!("Disconnect an unprotected outbound peer ({})", self.peer);
            let nc = Arc::clone(self.nc);
            self.synchronizer
                .shared()
                .shared()
                .async_handle()
                .spawn(async move {
                    let _ignore = nc
                        .async_disconnect(self.peer, "useless outbound peer in IBD")
                        .await;
                });
        }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L488-509)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let header = self.block.header();
        let actual_epoch_with_fraction = header.epoch();
        let block_number = header.number();
        let epoch_with_fraction = self.epoch.number_with_fraction(block_number);
        if actual_epoch_with_fraction != epoch_with_fraction {
            return Err(EpochError::NumberMismatch {
                expected: epoch_with_fraction.full_value(),
                actual: actual_epoch_with_fraction.full_value(),
            }
            .into());
        }
        let actual_compact_target = header.compact_target();
        if self.epoch.compact_target() != actual_compact_target {
            return Err(EpochError::TargetMismatch {
                expected: self.epoch.compact_target(),
                actual: actual_compact_target,
            }
            .into());
        }
        Ok(())
    }
```

**File:** sync/src/types/mod.rs (L1104-1140)
```rust
        let mut header_view = HeaderIndexView::new(
            header.hash(),
            header.number(),
            header.epoch(),
            header.timestamp(),
            parent_hash,
            parent_header_index.total_difficulty() + header.difficulty(),
        );

        let snapshot = Arc::clone(&self.shared.snapshot());
        header_view.build_skip(
            tip_number,
            |hash, store_first| self.get_header_index_view(hash, store_first),
            |number, current| {
                // shortcut to return an ancestor block
                if current.number <= snapshot.tip_number() && snapshot.is_main_chain(&current.hash)
                {
                    snapshot
                        .get_block_hash(number)
                        .and_then(|hash| self.get_header_index_view(&hash, true))
                } else {
                    None
                }
            },
        );
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
        if header_view.number().is_multiple_of(10000) {
            info!(
                "inserted valid header: header {}-{}",
                header_view.number(),
                header_view.hash()
            );
        }
        self.state.may_set_shared_best_header(header_view);
```

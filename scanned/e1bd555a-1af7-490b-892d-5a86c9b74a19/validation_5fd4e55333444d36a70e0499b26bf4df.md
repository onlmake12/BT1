### Title
Missing Validation of `ProposalWindow` Parameter Ordering in `ConsensusBuilder` Permanently Disables Two-Phase Transaction Commitment — (`spec/src/consensus.rs`)

---

### Summary

The `ConsensusBuilder::tx_proposal_window()` setter and `build()` method accept a `ProposalWindow(closest, farthest)` without validating that `closest < farthest`. If `closest >= farthest`, the two-phase commit mechanism is permanently broken: no non-cellbase transaction can ever be committed to the chain. There is no post-deployment mechanism to correct this.

---

### Finding Description

`ProposalWindow` encodes two consensus parameters — `w_close` (`closest`) and `w_far` (`farthest`) — that define the valid block-distance range between a transaction's proposal and its commitment. The protocol requires `w_close <= h_c − h_p <= w_far`. [1](#0-0) 

The `ConsensusBuilder::tx_proposal_window()` setter at line 430 accepts any `ProposalWindow` value with no ordering check: [2](#0-1) 

The `build()` method performs several `debug_assert!` checks (genesis difficulty, cellbase witness, epoch reward, epoch duration), but **no check that `closest < farthest`**: [3](#0-2) 

If `closest > farthest`, two downstream components break permanently:

**1. `TwoPhaseCommitVerifier::verify()`** computes:
```
proposal_start = block_number - farthest
proposal_end   = block_number - closest
```
When `closest > farthest`, `proposal_end < proposal_start`. The `while proposal_end >= proposal_start` loop never executes, `proposal_txs_ids` is always empty, and every non-cellbase transaction fails with `CommitError::Invalid`. [4](#0-3) 

**2. `ProposalWindow::length()`** computes `self.1 - self.0 + 1` (unsigned arithmetic). If `closest > farthest`, this underflows — panicking in debug builds or wrapping to a huge value in release builds. [5](#0-4) 

**3. `ProposalTable::finalize()`** uses the same `proposal_start`/`proposal_end` range logic. With an inverted window, the BTreeMap range query returns nothing, so no proposals ever enter the committable set. [6](#0-5) 

---

### Impact Explanation

If `closest >= farthest` is set in the `ConsensusBuilder`, the two-phase commit mechanism is permanently disabled. Every block containing non-cellbase transactions is rejected by `TwoPhaseCommitVerifier` with `CommitError::Invalid`. The chain can only produce empty blocks (cellbase only). There is no runtime mechanism to reset `tx_proposal_window` after the `Consensus` is built and the node is running — the parameter is baked into the `Consensus` struct at startup. [7](#0-6) 

---

### Likelihood Explanation

The `ConsensusBuilder` is a public API used by developers building custom or dev-mode CKB chains. The `tx_proposal_window()` setter is documented and intended for use. A developer who accidentally swaps `closest` and `farthest` (e.g., `ProposalWindow(10, 2)` instead of `ProposalWindow(2, 10)`) receives no compile-time or runtime error from `build()`. The `debug_assert!` guards in `build()` cover other parameters but not this one. The `Consensus` struct's fields are all `pub`, making direct construction with an inverted window equally possible. [8](#0-7) 

---

### Recommendation

- **Short term**: Add a validation check in `ConsensusBuilder::build()` (and optionally in `tx_proposal_window()`) asserting `closest < farthest` and `closest >= 1`. Use a hard `assert!` (not `debug_assert!`) so it fires in release builds:
  ```rust
  assert!(
      self.inner.tx_proposal_window.closest() < self.inner.tx_proposal_window.farthest(),
      "tx_proposal_window: closest must be strictly less than farthest"
  );
  ```
- **Long term**: Consider making `ProposalWindow::new(closest, farthest)` a validated constructor that returns `Result` or panics on invalid input, replacing the bare tuple struct construction `ProposalWindow(a, b)`.

---

### Proof of Concept

```rust
// Inverted window: closest (10) > farthest (2)
let consensus = ConsensusBuilder::default()
    .tx_proposal_window(ProposalWindow(10, 2))
    .build(); // No error, no panic in release mode

// At block height H > 10:
//   proposal_start = H - 2
//   proposal_end   = H - 10   <-- less than proposal_start
//
// TwoPhaseCommitVerifier: while proposal_end >= proposal_start → never runs
// proposal_txs_ids = {} (empty)
// committed_ids.difference(&{}) = committed_ids (non-empty)
// → CommitError::Invalid for every block with transactions
//
// ProposalWindow::length() = 2 - 10 + 1 → underflow panic (debug) or ~u64::MAX (release)
``` [2](#0-1) [9](#0-8)

### Citations

**File:** spec/src/consensus.rs (L107-153)
```rust
/// The struct represent CKB two-step-transaction-confirmation params
///
/// [two-step-transaction-confirmation params](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0020-ckb-consensus-protocol/0020-ckb-consensus-protocol.md#two-step-transaction-confirmation)
///
/// The `ProposalWindow` consists of two fields:
/// self.0, aka w_close, self.1, aka w_far
/// w_close and w_far define the closest and farthest on-chain distance between a transaction’s proposal and commitment.
#[derive(Clone, PartialEq, Debug, Eq, Copy)]
pub struct ProposalWindow(pub BlockNumber, pub BlockNumber);

/// "TYPE_ID" in hex
pub const TYPE_ID_CODE_HASH: H256 = h256!("0x545950455f4944");

/// Two protocol parameters w_close and w_far define the closest
/// and farthest on-chain distance between a transaction's proposal
/// and commitment.
///
/// A non-cellbase transaction is committed at height h_c if all of the following conditions are met:
/// 1) it is proposed at height h_p of the same chain, where w_close <= h_c − h_p <= w_far ;
/// 2) it is in the commitment zone of the main chain block with height h_c ;
///
/// ```text
/// ProposalWindow (2, 10)
///     propose
///        \
///         \
///         13 14 [15 16 17 18 19 20 21 22 23]
///                \_______________________/
///                             \
///                           commit
/// ```
///
impl ProposalWindow {
    /// The w_close parameter
    pub const fn closest(&self) -> BlockNumber {
        self.0
    }

    /// The w_far parameter
    pub const fn farthest(&self) -> BlockNumber {
        self.1
    }

    /// The proposal window length
    pub const fn length(&self) -> BlockNumber {
        self.1 - self.0 + 1
    }
```

**File:** spec/src/consensus.rs (L317-365)
```rust
    /// Build a new Consensus by taking ownership of the `Builder`, and returns a [`Consensus`].
    pub fn build(mut self) -> Consensus {
        debug_assert!(
            self.inner.genesis_block.difficulty() > U256::zero(),
            "genesis difficulty should greater than zero"
        );
        debug_assert!(
            !self.inner.genesis_block.data().transactions().is_empty()
                && !self
                    .inner
                    .genesis_block
                    .data()
                    .transactions()
                    .get(0)
                    .unwrap()
                    .witnesses()
                    .is_empty(),
            "genesis block must contain the witness for cellbase"
        );

        debug_assert!(
            self.inner.initial_primary_epoch_reward != Capacity::zero(),
            "initial_primary_epoch_reward must be non-zero"
        );

        debug_assert!(
            self.inner.epoch_duration_target() != 0,
            "epoch_duration_target must be non-zero"
        );

        debug_assert!(
            !self.inner.genesis_block.transactions().is_empty()
                && !self.inner.genesis_block.transactions()[0]
                    .witnesses()
                    .is_empty(),
            "genesis block must contain the witness for cellbase"
        );

        self.inner.dao_type_hash = self.get_type_hash(OUTPUT_INDEX_DAO).unwrap_or_default();
        self.inner.secp256k1_blake160_sighash_all_type_hash =
            self.get_type_hash(OUTPUT_INDEX_SECP256K1_BLAKE160_SIGHASH_ALL);
        self.inner.secp256k1_blake160_multisig_all_type_hash =
            self.get_type_hash(OUTPUT_INDEX_SECP256K1_BLAKE160_MULTISIG_ALL);
        self.inner
            .genesis_epoch_ext
            .set_compact_target(self.inner.genesis_block.compact_target());
        self.inner.genesis_hash = self.inner.genesis_block.hash();
        self.inner
    }
```

**File:** spec/src/consensus.rs (L429-433)
```rust
    /// Sets tx_proposal_window for the new Consensus.
    pub fn tx_proposal_window(mut self, proposal_window: ProposalWindow) -> Self {
        self.inner.tx_proposal_window = proposal_window;
        self
    }
```

**File:** spec/src/consensus.rs (L544-546)
```rust
    pub epoch_duration_target: u64,
    /// The two-step-transaction-confirmation proposal window
    pub tx_proposal_window: ProposalWindow,
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L146-214)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if self.block.is_genesis() {
            return Ok(());
        }
        let block_number = self.block.header().number();
        let proposal_window = self.context.consensus.tx_proposal_window();
        let proposal_start = block_number.saturating_sub(proposal_window.farthest());
        let mut proposal_end = block_number.saturating_sub(proposal_window.closest());

        let mut block_hash = self
            .context
            .store
            .get_block_hash(proposal_end)
            .ok_or(CommitError::AncestorNotFound)?;

        let mut proposal_txs_ids = HashSet::new();

        while proposal_end >= proposal_start {
            let header = self
                .context
                .store
                .get_block_header(&block_hash)
                .ok_or(CommitError::AncestorNotFound)?;
            if header.is_genesis() {
                break;
            }

            if let Some(ids) = self.context.store.get_block_proposal_txs_ids(&block_hash) {
                proposal_txs_ids.extend(ids);
            }
            if let Some(uncles) = self.context.store.get_block_uncles(&block_hash) {
                uncles
                    .data()
                    .into_iter()
                    .for_each(|uncle| proposal_txs_ids.extend(uncle.proposals()));
            }

            block_hash = header.data().raw().parent_hash();
            proposal_end -= 1;
        }

        let committed_ids: HashSet<_> = self
            .block
            .transactions()
            .iter()
            .skip(1)
            .map(TransactionView::proposal_short_id)
            .collect();

        if committed_ids.difference(&proposal_txs_ids).next().is_some() {
            error_target!(
                crate::LOG_TARGET,
                "BlockView {} {}",
                self.block.number(),
                self.block.hash()
            );
            error_target!(crate::LOG_TARGET, "proposal_window {:?}", proposal_window);
            error_target!(crate::LOG_TARGET, "Committed Ids:");
            for committed_id in committed_ids.iter() {
                error_target!(crate::LOG_TARGET, "    {:?}", committed_id);
            }
            error_target!(crate::LOG_TARGET, "Proposal Txs Ids:");
            for proposal_txs_id in proposal_txs_ids.iter() {
                error_target!(crate::LOG_TARGET, "    {:?}", proposal_txs_id);
            }
            return Err((CommitError::Invalid).into());
        }
        Ok(())
    }
```

**File:** util/proposal-table/src/lib.rs (L93-140)
```rust
    pub fn finalize(
        &mut self,
        origin: &ProposalView,
        number: BlockNumber,
    ) -> (HashSet<ProposalShortId>, ProposalView) {
        let candidate_number = number + 1;
        let proposal_start = candidate_number.saturating_sub(self.proposal_window.farthest());
        let proposal_end = candidate_number.saturating_sub(self.proposal_window.closest());

        if proposal_start > 1 {
            self.table = self.table.split_off(&proposal_start);
        }

        ckb_logger::trace!("[proposal_finalize] table {:?}", self.table);

        // - if candidate_number <= self.proposal_window.closest()
        //      new_ids = []
        //      gap = [1..candidate_number]
        // - else
        //      new_ids = [candidate_number- farthest..= candidate_number- closest]
        //      gap = [candidate_number- closest + 1..candidate_number]
        // - end
        let (new_ids, gap) = if candidate_number <= self.proposal_window.closest() {
            (
                HashSet::new(),
                self.table
                    .range((Bound::Unbounded, Bound::Included(&number)))
                    .flat_map(|pair| pair.1)
                    .cloned()
                    .collect(),
            )
        } else {
            (
                self.table
                    .range((
                        Bound::Included(&proposal_start),
                        Bound::Included(&proposal_end),
                    ))
                    .flat_map(|pair| pair.1)
                    .cloned()
                    .collect(),
                self.table
                    .range((Bound::Excluded(&proposal_end), Bound::Included(&number)))
                    .flat_map(|pair| pair.1)
                    .cloned()
                    .collect(),
            )
        };
```

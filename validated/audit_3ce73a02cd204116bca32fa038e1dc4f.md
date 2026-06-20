### Title
Epoch-based `since` verification uses phase-independent tip epoch instead of proposal-window-adjusted earliest-commit epoch, causing premature rejection of valid transactions — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`SinceVerifier` applies an inconsistent reference point when checking the three `since` metric types. Block-number-based `since` correctly uses `tx_env.block_number(proposal_window)` — the **earliest block the transaction can be committed in** — while epoch-based `since` uses `tx_env.epoch()` — the **raw tip epoch, identical for every phase**. This means a transaction with an epoch-based time lock is rejected with `Immature` even when it would be valid at the earliest possible commit block, while an equivalent block-number-locked transaction at the same chain position would be accepted.

---

### Finding Description

In `SinceVerifier::verify_absolute_lock` and `verify_relative_lock`, three `since` metric types are handled:

**Block-number (absolute and relative)** — uses the proposal-window-adjusted earliest commit block:

```rust
// verify_absolute_lock, line 636-638
let proposal_window = self.consensus.tx_proposal_window();
if self.tx_env.block_number(proposal_window) < block_number {
    return Err((TransactionError::Immature { index }).into());
}
```

```rust
// verify_relative_lock, line 679-685
let proposal_window = self.consensus.tx_proposal_window();
let required_block_number = info.block_number.checked_add(block_number)...;
if self.tx_env.block_number(proposal_window) < required_block_number {
    return Err((TransactionError::Immature { index }).into());
}
``` [1](#0-0) [2](#0-1) 

**Epoch (absolute and relative)** — uses the raw tip epoch, ignoring the proposal window entirely:

```rust
// verify_absolute_lock, line 645
let a = self.tx_env.epoch().to_rational();
```

```rust
// verify_relative_lock, line 692
let a = self.tx_env.epoch().to_rational();
``` [3](#0-2) [4](#0-3) 

`TxVerifyEnv::epoch()` is defined as:

```rust
/// The earliest epoch which the transaction will committed in.
pub fn epoch(&self) -> EpochNumberWithFraction {
    self.epoch   // always returns the raw tip epoch, regardless of phase
}
``` [5](#0-4) 

The comment is misleading. For the `Submitted` and `Proposed` phases, `self.epoch` is the **tip's** epoch, not the earliest commit epoch. By contrast, `block_number(proposal_window)` correctly adds `1 + proposal_window.closest()` for the `Submitted` phase:

```rust
pub fn block_number(&self, proposal_window: ProposalWindow) -> BlockNumber {
    match self.phase {
        TxVerifyPhase::Submitted => self.number + 1 + proposal_window.closest(),
        TxVerifyPhase::Proposed(already_proposed) => {
            self.number.saturating_sub(already_proposed) + proposal_window.closest()
        }
        TxVerifyPhase::Committed => self.number,
    }
}
``` [6](#0-5) 

A proposal-window-aware epoch accessor already exists — `epoch_number(proposal_window)` — but is **never called** in the epoch-based since checks:

```rust
pub fn epoch_number(&self, proposal_window: ProposalWindow) -> EpochNumber {
    let n_blocks = match self.phase {
        TxVerifyPhase::Submitted => 1 + proposal_window.closest(),
        TxVerifyPhase::Proposed(already_proposed) => {
            proposal_window.closest().saturating_sub(already_proposed)
        }
        TxVerifyPhase::Committed => 0,
    };
    self.epoch.minimum_epoch_number_after_n_blocks(n_blocks)
}
``` [7](#0-6) 

Notably, `epoch_number(proposal_window)` **is** used in the relative-timestamp branch for the hardfork check (line 706), making the omission in the epoch branch even more conspicuous: [8](#0-7) 

The `TxStatus::with_env` mapping shows the three phases that feed into `SinceVerifier`:

```rust
impl TxStatus {
    fn with_env(self, header: &HeaderView) -> TxVerifyEnv {
        match self {
            TxStatus::Fresh    => TxVerifyEnv::new_submit(header),
            TxStatus::Gap      => TxVerifyEnv::new_proposed(header, 0),
            TxStatus::Proposed => TxVerifyEnv::new_proposed(header, 1),
        }
    }
}
``` [9](#0-8) 

For `TxStatus::Fresh` (the common submission path), `block_number(proposal_window)` = `tip_number + 1 + proposal_window.closest()`, while `epoch()` = tip epoch. These diverge whenever an epoch boundary falls within the proposal window.

---

### Impact Explanation

**Scenario**: The default proposal window is `(2, 10)`. A user holds a DAO withdrawal transaction with `since = epoch 5` (absolute epoch lock). The current tip is at epoch 4, block 9 of 10 — one block before the epoch boundary. The earliest commit block is `tip_number + 3`, which falls inside epoch 5.

- With **block-number since** at the equivalent block: `block_number(proposal_window) = tip_number + 3 ≥ required` → **accepted**.
- With **epoch since**: `epoch() = epoch 4 < epoch 5` → **rejected with `Immature`**.

The user receives an unexpected `Immature` rejection and must wait one additional block (until the tip crosses into epoch 5) before resubmitting. This is the direct CKB analog of HMX's third problem: "being unable to call `increasePosition()` at all" because the mechanism is applied inconsistently across metric types.

Additionally, a transaction already in the `Gap` pool (proposed but not yet in the commit window) is re-verified with `TxVerifyEnv::new_proposed(header, 0)`. For epoch since, `epoch()` still returns the raw tip epoch, so a tx that was accepted into the pool near an epoch boundary could be re-rejected during the gap re-verification if the epoch has not yet advanced, even though the commit block is guaranteed to be within the valid epoch. [10](#0-9) 

---

### Likelihood Explanation

Epoch-based `since` locks are the standard mechanism for DAO withdrawals in CKB. Every DAO withdrawal transaction carries a relative epoch lock computed from the deposit epoch. Epoch boundaries are predictable and occur regularly. Any user who submits a DAO withdrawal (or any other epoch-locked transaction) within `proposal_window.closest()` blocks of the epoch boundary will hit this rejection. This is a realistic, externally reachable condition triggered by any unprivileged RPC caller via `send_transaction`.

---

### Recommendation

Replace `self.tx_env.epoch()` with a proposal-window-aware epoch comparison in both `verify_absolute_lock` and `verify_relative_lock`. The existing `epoch_number(proposal_window)` method provides the correct earliest-commit epoch number. The comparison should be restructured to compare the minimum epoch number after the proposal window against the required epoch number, consistent with how `block_number(proposal_window)` is used for block-number since. [3](#0-2) [4](#0-3) 

---

### Proof of Concept

1. Configure a CKB node with default proposal window `(2, 10)` and epoch length 10.
2. Mine to block 9 of epoch 4 (tip = epoch 4, index 9/10).
3. Construct a transaction spending a live cell with `since = 0x2000_0001_0000_0005` (absolute epoch 5, index 0/1).
4. Submit via `send_transaction` RPC.
5. **Observed**: `TransactionFailedToVerify: Verification failed Transaction(Immature(...))`
6. Mine one more block (tip advances to epoch 5, block 0/10).
7. Resubmit the same transaction.
8. **Observed**: accepted into the pending pool.

The transaction is valid at the earliest commit block (tip + 3, which is epoch 5) in step 4, but `epoch()` returns epoch 4 < epoch 5, causing the premature rejection. An equivalent transaction with `since = block N` (where N = tip_number + 3) would be accepted at step 4 because `block_number(proposal_window) = tip_number + 3 ≥ N`. [11](#0-10) [12](#0-11)

### Citations

**File:** verification/src/transaction_verifier.rs (L632-664)
```rust
    fn verify_absolute_lock(&self, index: usize, since: Since) -> Result<(), Error> {
        if since.is_absolute() {
            match since.extract_metric() {
                Some(SinceMetric::BlockNumber(block_number)) => {
                    let proposal_window = self.consensus.tx_proposal_window();
                    if self.tx_env.block_number(proposal_window) < block_number {
                        return Err((TransactionError::Immature { index }).into());
                    }
                }
                Some(SinceMetric::EpochNumberWithFraction(epoch_number_with_fraction)) => {
                    if !epoch_number_with_fraction.is_well_formed_increment() {
                        return Err((TransactionError::InvalidSince { index }).into());
                    }
                    let a = self.tx_env.epoch().to_rational();
                    let b = epoch_number_with_fraction.normalize().to_rational();
                    if a < b {
                        return Err((TransactionError::Immature { index }).into());
                    }
                }
                Some(SinceMetric::Timestamp(timestamp)) => {
                    let parent_hash = self.tx_env.parent_hash();
                    let tip_timestamp = self.block_median_time(&parent_hash);
                    if tip_timestamp < timestamp {
                        return Err((TransactionError::Immature { index }).into());
                    }
                }
                None => {
                    return Err((TransactionError::InvalidSince { index }).into());
                }
            }
        }
        Ok(())
    }
```

**File:** verification/src/transaction_verifier.rs (L678-686)
```rust
                Some(SinceMetric::BlockNumber(block_number)) => {
                    let proposal_window = self.consensus.tx_proposal_window();
                    let required_block_number = info
                        .block_number
                        .checked_add(block_number)
                        .ok_or(TransactionError::InvalidSince { index })?;
                    if self.tx_env.block_number(proposal_window) < required_block_number {
                        return Err((TransactionError::Immature { index }).into());
                    }
```

**File:** verification/src/transaction_verifier.rs (L688-697)
```rust
                Some(SinceMetric::EpochNumberWithFraction(epoch_number_with_fraction)) => {
                    if !epoch_number_with_fraction.is_well_formed_increment() {
                        return Err((TransactionError::InvalidSince { index }).into());
                    }
                    let a = self.tx_env.epoch().to_rational();
                    let b = info.block_epoch.to_rational()
                        + epoch_number_with_fraction.normalize().to_rational();
                    if a < b {
                        return Err((TransactionError::Immature { index }).into());
                    }
```

**File:** verification/src/transaction_verifier.rs (L704-706)
```rust
                    let proposal_window = self.consensus.tx_proposal_window();
                    let parent_hash = self.tx_env.parent_hash();
                    let epoch_number = self.tx_env.epoch_number(proposal_window);
```

**File:** script/src/verify_env.rs (L83-104)
```rust
    /// The block number of the earliest block which the transaction will committed in.
    pub fn block_number(&self, proposal_window: ProposalWindow) -> BlockNumber {
        match self.phase {
            TxVerifyPhase::Submitted => self.number + 1 + proposal_window.closest(),
            TxVerifyPhase::Proposed(already_proposed) => {
                self.number.saturating_sub(already_proposed) + proposal_window.closest()
            }
            TxVerifyPhase::Committed => self.number,
        }
    }

    /// The epoch number of the earliest epoch which the transaction will committed in.
    pub fn epoch_number(&self, proposal_window: ProposalWindow) -> EpochNumber {
        let n_blocks = match self.phase {
            TxVerifyPhase::Submitted => 1 + proposal_window.closest(),
            TxVerifyPhase::Proposed(already_proposed) => {
                proposal_window.closest().saturating_sub(already_proposed)
            }
            TxVerifyPhase::Committed => 0,
        };
        self.epoch.minimum_epoch_number_after_n_blocks(n_blocks)
    }
```

**File:** script/src/verify_env.rs (L116-119)
```rust
    /// The earliest epoch which the transaction will committed in.
    pub fn epoch(&self) -> EpochNumberWithFraction {
        self.epoch
    }
```

**File:** tx-pool/src/process.rs (L55-63)
```rust
impl TxStatus {
    fn with_env(self, header: &HeaderView) -> TxVerifyEnv {
        match self {
            TxStatus::Fresh => TxVerifyEnv::new_submit(header),
            TxStatus::Gap => TxVerifyEnv::new_proposed(header, 0),
            TxStatus::Proposed => TxVerifyEnv::new_proposed(header, 1),
        }
    }
}
```

**File:** tx-pool/src/util.rs (L134-148)
```rust
pub(crate) fn time_relative_verify(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: TxVerifyEnv,
) -> Result<(), Reject> {
    let consensus = snapshot.cloned_consensus();
    TimeRelativeTransactionVerifier::new(
        rtx,
        consensus,
        snapshot.as_data_loader(),
        Arc::new(tx_env),
    )
    .verify()
    .map_err(Reject::Verification)
}
```

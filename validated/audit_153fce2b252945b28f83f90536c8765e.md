### Title
`CapacityVerifier::verify()` Skips `OutputsSumOverflow` Check for All Transactions Containing Any DAO Input, Leaving Non-DAO Capacity Unguarded — (`File: verification/src/transaction_verifier.rs`)

### Summary

`CapacityVerifier::verify()` is documented to enforce that total inputs capacity ≥ total outputs capacity for every transaction. However, its implementation unconditionally skips this check for any transaction that contains even a single DAO-typed input cell, delegating the responsibility to the DAO type script. The DAO type script only enforces the DAO-specific withdrawal amount per cell; it does not enforce the global capacity balance of the transaction. For mixed transactions (DAO inputs + non-DAO inputs), neither the `CapacityVerifier` nor the DAO type script checks whether the non-DAO portion of outputs exceeds the non-DAO portion of inputs, creating a capacity accounting gap reachable by any unprivileged transaction sender.

### Finding Description

**Root cause — `valid_dao_withdraw_transaction()` is too broad:**

`CapacityVerifier::verify()` in `verification/src/transaction_verifier.rs` carries the following docstring:

```
/// Verify sum of inputs capacity should be greater than or equal to sum of outputs capacity
/// Verify outputs capacity should be greater than or equal to its occupied capacity
``` [1](#0-0) 

The implementation then immediately contradicts this contract:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(TransactionError::OutputsSumOverflow { ... }.into());
    }
}
``` [2](#0-1) 

The guard `valid_dao_withdraw_transaction()` uses `.any()`:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [3](#0-2) 

This returns `true` — and therefore skips the entire `OutputsSumOverflow` check — whenever **any** input carries the DAO type script, regardless of how many non-DAO inputs are also present.

**Why the DAO type script does not fill the gap:**

The inline comment justifying the skip states: *"DAO withdraw transaction is verified via the type script of DAO cells."* [4](#0-3) 

However, `DaoCalculator::transaction_maximum_withdraw()` shows that for non-DAO inputs the calculator simply returns `output.capacity().into()` — the raw cell capacity, not any interest-adjusted amount:

```rust
} else {
    Ok(output.capacity().into())
}
``` [5](#0-4) 

The DAO type script (a RISC-V script running in CKB-VM) only verifies that each DAO output capacity ≤ `calculate_maximum_withdraw(deposit_cell, deposit_header, withdraw_header)` for its own associated cell. It has no visibility into the aggregate capacity balance of the transaction and cannot enforce that non-DAO outputs do not exceed non-DAO inputs.

**Exploit path:**

1. Attacker owns a small DAO cell (deposit or withdraw phase 1) and one or more non-DAO cells.
2. Attacker constructs a transaction:
   - Input 0: DAO cell (e.g., 100 CKB, max withdrawal = 110 CKB)
   - Input 1: non-DAO cell (e.g., 50 CKB)
   - Output 0: DAO output (110 CKB — passes DAO type script)
   - Output 1: non-DAO output (90 CKB — 40 CKB more than the non-DAO input)
3. `valid_dao_withdraw_transaction()` returns `true` because Input 0 is a DAO cell.
4. `CapacityVerifier` skips the `OutputsSumOverflow` check entirely.
5. The DAO type script passes (110 CKB ≤ 110 CKB max withdrawal).
6. No verifier checks that total outputs (200 CKB) > total inputs (150 CKB) + DAO interest (10 CKB) = 160 CKB.
7. The transaction is admitted to the tx-pool via the `send_transaction` RPC.

**Secondary backstop — `DaoCalculator::transaction_fee()`:**

`DaoCalculator::transaction_fee()` computes `maximum_withdraw - outputs_capacity` and would fail via `safe_sub` if outputs exceed the maximum:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))
        .map_err(Into::into)
}
``` [6](#0-5) 

If this function is called as a hard validation step during contextual block verification (the contextual block verifier imports `DaoCalculator`), a block containing such a transaction would be rejected, limiting the impact to **tx-pool DoS** rather than on-chain capacity inflation. However, the tx-pool admission path runs `CapacityVerifier` (which skips the check) and the DAO type script (which only checks the DAO cell), so the transaction is admitted to the pool regardless. [7](#0-6) 

### Impact Explanation

**Confirmed impact — tx-pool resource exhaustion (DoS):** Any unprivileged transaction sender can submit an unbounded number of transactions that pass tx-pool admission but can never be included in a valid block. Each such transaction consumes tx-pool memory and CPU (script execution, resolution). Legitimate transactions may be evicted or delayed.

**Potential impact — on-chain capacity inflation:** If `DaoCalculator::transaction_fee()` is not enforced as a hard validation gate during block verification (or if a miner bypasses it), the non-DAO capacity surplus in outputs would represent CKB created from nothing, violating the conservation invariant. This would be a critical consensus-breaking issue. The exact severity depends on the block verification pipeline, which could not be fully traced within the available search iterations.

### Likelihood Explanation

The attack requires only:
- Owning any live DAO cell (deposit or withdraw phase 1) — achievable by any CKB holder.
- Owning any non-DAO cell — trivially available.
- Submitting a crafted transaction via the public `send_transaction` RPC.

No privileged access, no majority hashpower, no social engineering. Likelihood is **high** for the tx-pool DoS vector.

### Recommendation

Replace the broad `.any()` guard in `valid_dao_withdraw_transaction()` with a check that only exempts the `OutputsSumOverflow` check when **all** inputs are DAO cells (a pure DAO withdrawal), or — preferably — remove the blanket skip entirely and instead compare `inputs_sum + dao_interest >= outputs_sum` using `DaoCalculator::transaction_maximum_withdraw()` directly inside `CapacityVerifier::verify()`. This would make the implementation match its documented contract for all transaction shapes.

### Proof of Concept

```
Transaction:
  inputs:
    [0] DAO cell (type = DAO type script, capacity = 100 CKB, data = deposit block number)
    [1] non-DAO cell (capacity = 50 CKB)
  outputs:
    [0] non-DAO cell (capacity = 110 CKB)   ← DAO interest absorbed here
    [1] non-DAO cell (capacity = 90 CKB)    ← 40 CKB surplus, unchecked

Verification trace:
  valid_dao_withdraw_transaction() → true  (input[0] has DAO type script)
  CapacityVerifier skips OutputsSumOverflow check
  DAO type script: not triggered on output[0] (output[0] has no DAO type script)
  CapacityVerifier::InsufficientCellCapacity: not triggered (each output ≥ occupied)
  Result: ACCEPTED by tx-pool

  Total inputs  = 150 CKB
  Total outputs = 200 CKB
  Surplus       = 50 CKB — never validated by any active verifier at admission time
``` [8](#0-7) [9](#0-8)

### Citations

**File:** verification/src/transaction_verifier.rs (L461-523)
```rust
/// Perform inputs and outputs `capacity` field related verification
pub struct CapacityVerifier {
    resolved_transaction: Arc<ResolvedTransaction>,
    dao_type_hash: Byte32,
}

impl CapacityVerifier {
    /// Create a new `CapacityVerifier`
    pub fn new(resolved_transaction: Arc<ResolvedTransaction>, dao_type_hash: Byte32) -> Self {
        CapacityVerifier {
            resolved_transaction,
            dao_type_hash,
        }
    }

    /// Verify sum of inputs capacity should be greater than or equal to sum of outputs capacity
    /// Verify outputs capacity should be greater than or equal to its occupied capacity
    pub fn verify(&self) -> Result<(), Error> {
        // skip OutputsSumOverflow verification for resolved cellbase and DAO
        // withdraw transactions.
        // cellbase's outputs are verified by RewardVerifier
        // DAO withdraw transaction is verified via the type script of DAO cells
        if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
            let inputs_sum = self.resolved_transaction.inputs_capacity()?;
            let outputs_sum = self.resolved_transaction.outputs_capacity()?;

            if inputs_sum < outputs_sum {
                return Err((TransactionError::OutputsSumOverflow {
                    inputs_sum,
                    outputs_sum,
                })
                .into());
            }
        }

        for (index, (output, data)) in self
            .resolved_transaction
            .transaction
            .outputs_with_data_iter()
            .enumerate()
        {
            let data_occupied_capacity = Capacity::bytes(data.len())?;
            if output.is_lack_of_capacity(data_occupied_capacity)? {
                return Err((TransactionError::InsufficientCellCapacity {
                    index,
                    inner: TransactionErrorSource::Outputs,
                    capacity: output.capacity().into(),
                    occupied_capacity: output.occupied_capacity(data_occupied_capacity)?,
                })
                .into());
            }
        }

        Ok(())
    }

    fn valid_dao_withdraw_transaction(&self) -> bool {
        self.resolved_transaction
            .resolved_inputs
            .iter()
            .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
    }
}
```

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
```

**File:** util/dao/src/lib.rs (L38-124)
```rust
    fn transaction_maximum_withdraw(
        &self,
        rtx: &ResolvedTransaction,
    ) -> Result<Capacity, DaoError> {
        let header_deps: HashSet<Byte32> = rtx.transaction.header_deps_iter().collect();
        rtx.resolved_inputs.iter().enumerate().try_fold(
            Capacity::zero(),
            |capacities, (i, cell_meta)| {
                let capacity: Result<Capacity, DaoError> = {
                    let output = &cell_meta.cell_output;
                    let is_dao_type_script = |type_script: Script| {
                        Into::<u8>::into(type_script.hash_type())
                            == Into::<u8>::into(ScriptHashType::Type)
                            && type_script.code_hash() == self.consensus.dao_type_hash()
                    };
                    let is_dao_output = output
                        .type_()
                        .to_opt()
                        .map(is_dao_type_script)
                        .unwrap_or(false);
                    if is_dao_output {
                        // A withdrawing DAO cell has 8 bytes of cell data storing the
                        // block number of the original deposit.
                        let deposited_block_number =
                            match self.data_loader.load_cell_data(cell_meta) {
                                Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
                                _ => 0,
                            };
                        if deposited_block_number > 0 {
                            let withdrawing_header_hash = cell_meta
                                .transaction_info
                                .as_ref()
                                .map(|info| &info.block_hash)
                                .filter(|hash| header_deps.contains(hash))
                                .ok_or(DaoError::InvalidOutPoint)?;
                            let deposit_header_hash = rtx
                                .transaction
                                .witnesses()
                                .get(i)
                                .ok_or(DaoError::InvalidOutPoint)
                                .and_then(|witness_data| {
                                    // dao contract stores header deps index as u64 in the input_type field of WitnessArgs
                                    let witness =
                                        WitnessArgs::from_slice(&Into::<Bytes>::into(witness_data))
                                            .map_err(|_| DaoError::InvalidDaoFormat)?;
                                    let header_deps_index_data: Option<Bytes> =
                                        witness.input_type().to_opt().map(|witness| witness.into());
                                    if header_deps_index_data.is_none()
                                        || header_deps_index_data.clone().map(|data| data.len())
                                            != Some(8)
                                    {
                                        return Err(DaoError::InvalidDaoFormat);
                                    }
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
                                        .and_then(|hash| header_deps.get(&hash))
                                        .ok_or(DaoError::InvalidOutPoint)
                                })?;

                            let deposit_header = self
                                .data_loader
                                .get_header(deposit_header_hash)
                                .ok_or(DaoError::InvalidHeader)?;
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
                        } else {
                            Ok(output.capacity().into())
                        }
                    } else {
                        Ok(output.capacity().into())
                    }
                };
                capacity.and_then(|c| c.safe_add(capacities).map_err(Into::into))
            },
        )
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L1-36)
```rust
use crate::uncles_verifier::{UncleProvider, UnclesVerifier};
use ckb_async_runtime::Handle;
use ckb_chain_spec::{
    consensus::{Consensus, ConsensusProvider},
    versionbits::VersionbitsIndexer,
};
use ckb_dao::DaoCalculator;
use ckb_dao_utils::DaoError;
use ckb_error::{Error, InternalErrorKind};
use ckb_logger::error_target;
use ckb_merkle_mountain_range::MMRStore;
use ckb_reward_calculator::RewardCalculator;
use ckb_store::{ChainStore, data_loader_wrapper::AsDataLoader};
use ckb_traits::HeaderProvider;
use ckb_types::{
    core::error::OutPointError,
    core::{
        BlockReward, BlockView, Capacity, Cycle, EpochExt, HeaderView, TransactionView,
        cell::{HeaderChecker, ResolvedTransaction},
    },
    packed::{Byte32, CellOutput, HeaderDigest, Script},
    prelude::*,
    utilities::merkle_mountain_range::ChainRootMMR,
};
use ckb_verification::cache::{
    TxVerificationCache, {CacheEntry, Completed},
};
use ckb_verification::{
    BlockErrorKind, CellbaseError, CommitError, ContextualTransactionVerifier,
    DaoScriptSizeVerifier, TimeRelativeTransactionVerifier, UnknownParentError,
};
use ckb_verification::{BlockTransactionsError, EpochError, TxVerifyEnv};
use ckb_verification_traits::Switch;
use rayon::iter::{IndexedParallelIterator, IntoParallelRefIterator, ParallelIterator};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
```

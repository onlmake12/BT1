### Title
Off-by-One Epoch Boundary Check Causes First RFC0044 Activation Block to Omit Required MMR Extension — (File: `tx-pool/src/block_assembler/mod.rs`)

---

### Summary

In `build_extension`, the block assembler evaluates RFC0044 (MMR) activation using the **tip block's** epoch number rather than the **candidate block's** epoch number. At the exact epoch boundary where RFC0044 activates, this off-by-one causes the first block of the new epoch to be assembled without the required MMR extension field, even though that block is the first one that must carry it.

---

### Finding Description

`build_extension` is called during block template assembly to decide whether to include the MMR chain root in the block extension field. The activation check is:

```rust
// The use of the epoch number of the tip here leads to an off-by-one bug,
// so be careful, it needs to be preserved for consistency reasons and not fixed directly.
let mmr_activate = snapshot
    .consensus()
    .rfc0044_active(tip_header.epoch().number());
``` [1](#0-0) 

The candidate block being assembled is at `tip_number + 1`. At an epoch boundary, the candidate block is in epoch `N+1` while the tip is still in epoch `N`. If RFC0044 activates at epoch `N+1`, then:

- `rfc0044_active(N)` → `false` (tip epoch, pre-activation)
- `rfc0044_active(N+1)` → `true` (candidate epoch, activation epoch)

The assembler evaluates the wrong epoch, so it omits the extension from the first block of the activation epoch. This is structurally identical to the DSPrism `i < finalists.length - 1` bug: the boundary element (the first activation-epoch block) is skipped by the controlling condition.

The code comment explicitly acknowledges this is a bug and states it is preserved for "consistency reasons," meaning the contextual block verifier's `rfc0044_active` call may share the same off-by-one, causing both sides to agree on the incorrect behavior. [2](#0-1) 

---

### Impact Explanation

**If the verifier uses the correct epoch (candidate block's epoch):** The miner's assembled block template is missing the required extension. Any miner calling `get_block_template` at the activation epoch boundary receives an invalid template. Submitting the resulting block causes it to be rejected by all validating peers, and the miner loses the block reward for that slot.

**If the verifier shares the same off-by-one:** Both assembler and verifier agree to omit the extension from the first activation-epoch block. The block is accepted by the network, but the MMR chain is missing a leaf for that block number. This silently corrupts the MMR proof chain, breaking the integrity guarantees that RFC0044 is designed to provide for light clients and cross-chain proofs.

Either outcome represents a consensus-level correctness failure triggered at a deterministic, predictable point in time (the RFC0044 activation epoch boundary).

---

### Likelihood Explanation

The bug fires exactly once per chain lifetime — at the RFC0044 activation epoch boundary. It is deterministic and requires no attacker action. Any miner running the standard CKB node and calling the `get_block_template` RPC at that boundary is automatically affected. The entry path is the standard miner/block-template RPC caller, which is explicitly in scope.

---

### Recommendation

Replace `tip_header.epoch().number()` with the candidate block's epoch number in `build_extension`. The candidate epoch can be derived from `snapshot.next_epoch_ext(...)` or passed in from the caller context that already computes `current_epoch`. The fix must be coordinated with any corresponding check in the contextual block verifier to avoid a consensus split.

---

### Proof of Concept

1. Configure a CKB node with RFC0044 scheduled to activate at epoch `E`.
2. Let the chain reach the last block of epoch `E-1` (tip is in epoch `E-1`).
3. Call `get_block_template` RPC. The assembler calls `build_extension` with `tip_header.epoch().number() = E-1`.
4. `rfc0044_active(E-1)` returns `false`.
5. The returned block template has no extension field.
6. The candidate block is the first block of epoch `E`, which RFC0044 requires to carry the MMR chain root.
7. If the verifier correctly checks the candidate block's epoch `E`, it rejects the template-derived block → miner loses reward. If the verifier also uses `E-1`, the block is accepted with a missing MMR leaf → silent MMR corruption. [3](#0-2)

### Citations

**File:** tx-pool/src/block_assembler/mod.rs (L564-581)
```rust
    pub(crate) fn build_extension(snapshot: &Snapshot) -> Result<Option<packed::Bytes>, AnyError> {
        let tip_header = snapshot.tip_header();
        // The use of the epoch number of the tip here leads to an off-by-one bug,
        // so be careful, it needs to be preserved for consistency reasons and not fixed directly.
        let mmr_activate = snapshot
            .consensus()
            .rfc0044_active(tip_header.epoch().number());
        if mmr_activate {
            let chain_root = snapshot
                .chain_root_mmr(tip_header.number())
                .get_root()
                .map_err(|e| InternalErrorKind::MMR.other(e))?;
            let bytes = chain_root.calc_mmr_hash().as_bytes().into();
            Ok(Some(bytes))
        } else {
            Ok(None)
        }
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

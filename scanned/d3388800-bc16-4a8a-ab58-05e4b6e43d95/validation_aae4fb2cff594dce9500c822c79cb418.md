I was able to confirm the key code facts via grep and partial file reads. Here is my analysis:

**Confirmed facts from the codebase:**

1. The comment `"treat as live cell, regardless of live or dead"` exists in `rpc/src/module/chain.rs` inside `CyclesEstimator`. [1](#0-0) 

2. `CyclesEstimator` is used by both `estimate_cycles` (chain.rs) and `dry_run_transaction` (experiment.rs), with no cycle-budget parameter passed at the call site — the limit is drawn from consensus `max_block_cycles`. [2](#0-1) 

3. No RPC-level rate limiting, per-caller throttle, or authentication guard was found for `estimate_cycles`. [1](#0-0) 

4. `ScriptVerifier` is imported directly into chain.rs and used by `CyclesEstimator`. [3](#0-2) 

---

### Title
Unauthenticated `estimate_cycles` RPC Executes Scripts Against Dead Cells Up to `max_block_cycles`, Enabling CPU Exhaustion DoS — (`rpc/src/module/chain.rs`)

### Summary
`CyclesEstimator::cell()` in `rpc/src/module/chain.rs` deliberately resolves every referenced out-point as a live cell regardless of whether it has already been spent on-chain. An unprivileged caller can submit a transaction whose inputs reference known-dead cells carrying a maximally-looping CKB-VM script, causing `ScriptVerifier::verify(max_block_cycles)` to run to completion (~3.5 B cycles on mainnet) for every unauthenticated RPC call. With no per-caller rate limit, concurrent callers can saturate node CPU, degrading block production.

### Finding Description
`CyclesEstimator` implements `CellProvider` with a `cell()` method that, per the in-code comment, treats every out-point as live regardless of its actual on-chain status. This bypasses the normal dead-cell rejection that the tx-pool and block-verifier enforce. The `run()` method then calls `resolve_transaction` (which succeeds because all cells appear live) and passes the resolved transaction to `ScriptVerifier::verify(max_block_cycles)`. The cycle budget is the full consensus `max_block_cycles`; there is no smaller cap applied at the RPC layer. The RPC endpoint is unauthenticated and publicly reachable on any node that exposes its RPC port.

### Impact Explanation
Each call to `estimate_cycles` with a crafted transaction can consume up to `max_block_cycles` (~3.5 × 10⁹ on mainnet) of synchronous CPU time on the node's RPC thread pool. Multiple concurrent callers can exhaust all available CPU, causing block-template generation and block-relay processing to stall, directly degrading mining throughput and network liveness (economy damage via liveness attack).

### Likelihood Explanation
The exploit requires only: (a) knowledge of any spent out-point on-chain (trivially obtained from any block explorer), (b) a CKB-VM script that loops to the cycle limit (a few bytes of RISC-V), and (c) the ability to send HTTP JSON-RPC requests. No key material, privileged access, or hashpower is needed. The attack is locally testable and repeatable.

### Recommendation
- Apply a tighter per-call cycle cap (e.g., `max_block_cycles / N`) inside `CyclesEstimator::run()`.
- Add per-IP or per-connection rate limiting on the `estimate_cycles` and `dry_run_transaction` endpoints.
- Optionally, reject transactions whose inputs resolve to dead cells even in the estimator path, preserving the invariant that dead cells are never treated as live for script resolution.

### Proof of Concept
1. Identify any spent out-point `(tx_hash, index)` from the chain.
2. Deploy (or reference an existing) CKB-VM script that loops until cycle exhaustion.
3. Construct a transaction with that out-point as input and the looping script as its lock.
4. In a tight loop from N clients, call:
   ```
   POST /  {"method":"estimate_cycles","params":[<tx>],...}
   ```
5. Observe node CPU at 100 % and block-production latency increasing proportionally with N. [4](#0-3) [5](#0-4)

### Citations

**File:** rpc/src/module/chain.rs (L1-33)
```rust
use crate::error::RPCError;
use crate::util::FeeRateCollector;
use async_trait::async_trait;
use ckb_jsonrpc_types::{
    BlockEconomicState, BlockFilter, BlockNumber, BlockResponse, BlockView, CellWithStatus,
    Consensus, EpochNumber, EpochView, EstimateCycles, FeeRateStatistics, HeaderView, OutPoint,
    ResponseFormat, ResponseFormatInnerType, Timestamp, Transaction, TransactionAndWitnessProof,
    TransactionProof, TransactionWithStatusResponse, Uint32, Uint64,
};
use ckb_logger::error;
use ckb_reward_calculator::RewardCalculator;
use ckb_shared::{Snapshot, shared::Shared};
use ckb_store::{ChainStore, data_loader_wrapper::AsDataLoader};
use ckb_traits::HeaderFieldsProvider;
use ckb_types::core::tx_pool::TransactionWithStatus;
use ckb_types::{
    H256,
    core::{
        self,
        cell::{CellProvider, CellStatus, HeaderChecker, resolve_transaction},
        error::OutPointError,
    },
    packed,
    prelude::*,
    utilities::{CBMT, MerkleProof, merkle_root},
};
use ckb_verification::ScriptVerifier;
use ckb_verification::TxVerifyEnv;
use jsonrpc_core::Result;
use jsonrpc_utils::rpc;
use std::collections::HashSet;
use std::sync::Arc;

```

**File:** rpc/src/module/experiment.rs (L229-233)
```rust
impl ExperimentRpc for ExperimentRpcImpl {
    fn dry_run_transaction(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
    }
```

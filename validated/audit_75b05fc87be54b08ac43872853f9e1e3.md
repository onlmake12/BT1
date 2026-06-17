### Title
`SetValidPeriod` Governance Action Always Panics on Starknet, Permanently Blocking Valid Period Updates — (`target_chains/starknet/contracts/src/pyth/governance.cairo`)

### Summary

The Starknet Pyth contract's `parse_instruction` function contains a hardcoded `panic_with_felt252('unimplemented')` for the `SetValidPeriod` governance action. Any attempt to execute a valid, guardian-signed `SetValidPeriod` governance VAA on Starknet will always revert, permanently preventing governance from adjusting the price staleness threshold on that chain.

### Finding Description

In `target_chains/starknet/contracts/src/pyth/governance.cairo`, the `parse_instruction` function dispatches on `GovernanceAction`. The `SetValidPeriod` arm unconditionally panics:

```cairo
GovernanceAction::SetValidPeriod => { panic_with_felt252('unimplemented') },
``` [1](#0-0) 

This is called from `execute_governance_instruction` in `target_chains/starknet/contracts/src/pyth.cairo`, which is a public entry point callable by any transaction sender:

```cairo
fn execute_governance_instruction(ref self: ContractState, data: ByteBuffer) {
    ...
    let instruction = governance::parse_instruction(vm.payload);
    ...
    match instruction.payload { ... }
}
``` [2](#0-1) 

Every other supported Pyth chain correctly implements `SetValidPeriod`. For example, the EVM contract handles it:

```solidity
} else if (gi.action == GovernanceAction.SetValidPeriod) {
    setValidPeriod(parseSetValidPeriodPayload(gi.payload));
}
``` [3](#0-2) 

The Fuel contract handles it, the CosmWasm contract handles it, and the Stylus contract handles it — but Starknet panics. [4](#0-3) [5](#0-4) 

### Impact Explanation

The `validTimePeriodSeconds` parameter controls how long a price update is considered non-stale. If governance needs to adjust this on Starknet (e.g., to tighten staleness checks after a security review, or to accommodate a chain with different block times), the transaction will always revert. The valid period is frozen at its deployment value with no on-chain path to change it. Protocols on Starknet consuming Pyth prices could be exposed to stale price risk if the current valid period is misconfigured, with no remediation path short of a full contract upgrade.

### Likelihood Explanation

No preconditions beyond governance wanting to use `SetValidPeriod` on Starknet. The bug is inevitable — any valid, guardian-signed `SetValidPeriod` VAA submitted to the Starknet contract will always panic. The `SetValidPeriod` action is a standard, actively used governance action across all other Pyth chains.

### Recommendation

Implement `SetValidPeriod` in the Starknet governance parser and executor, mirroring the EVM implementation:

```cairo
GovernanceAction::SetValidPeriod => {
    let new_period = reader.read_u64();
    GovernancePayload::SetValidPeriod(SetValidPeriod { new_period })
},
```

And in `execute_governance_instruction`:

```cairo
GovernancePayload::SetValidPeriod(payload) => {
    self.valid_time_period_seconds.write(payload.new_period);
    // emit event
},
```

### Proof of Concept

1. Governance creates a `SetValidPeriod` VAA targeting Starknet's chain ID, signed by the current guardian set.
2. Any transaction sender calls `execute_governance_instruction(vaa)` on the Starknet Pyth contract.
3. `parse_instruction` is called on the VAA payload.
4. The `GovernanceAction::SetValidPeriod` arm is reached and `panic_with_felt252('unimplemented')` is executed.
5. The transaction reverts. The valid period is unchanged. The action cannot be retried with any different input — the panic is unconditional. [1](#0-0)

### Citations

**File:** target_chains/starknet/contracts/src/pyth/governance.cairo (L184-184)
```text
        GovernanceAction::SetValidPeriod => { panic_with_felt252('unimplemented') },
```

**File:** target_chains/starknet/contracts/src/pyth.cairo (L412-416)
```text
        fn execute_governance_instruction(ref self: ContractState, data: ByteBuffer) {
            let wormhole = IWormholeDispatcher { contract_address: self.wormhole_address.read() };
            let vm = wormhole.parse_and_verify_vm(data.clone());
            self.verify_governance_vm(@vm);
            let instruction = governance::parse_instruction(vm.payload);
```

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol (L88-90)
```text
        } else if (gi.action == GovernanceAction.SetValidPeriod) {
            setValidPeriod(parseSetValidPeriodPayload(gi.payload));
        } else if (
```

**File:** target_chains/fuel/contracts/pyth-contract/src/main.sw (L894-898)
```text
        GovernanceAction::SetValidPeriod => {
            let svp = GovernanceInstruction::parse_set_valid_period_payload(gi.payload);
            log(svp);
            set_valid_period(svp);
        },
```

**File:** target_chains/cosmwasm/contracts/pyth/src/governance.rs (L64-64)
```rust
    SetValidPeriod { valid_seconds: u64 }, // 4
```

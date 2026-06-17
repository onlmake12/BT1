### Title
Reentrancy in `Executor.execute()` Allows Skipping Intermediate Governance Instructions - (File: `target_chains/ethereum/contracts/contracts/executor/Executor.sol`)

### Summary

`Executor.execute()` is a `public payable` function that makes an arbitrary external call to a governance-specified `callAddress`. Although `lastExecutedSequence` is updated before the external call (partial CEI), there is no reentrancy guard. A reentrant call with a higher-sequence VAA during the external call permanently blocks any intermediate-sequence VAAs from ever executing.

### Finding Description

`Executor.execute()` calls `verifyGovernanceVM()` which updates `lastExecutedSequence` before the external call, then immediately makes an unconstrained external call:

```solidity
// verifyGovernanceVM (called first):
lastExecutedSequence = vm.sequence;   // state updated before external call

// back in execute():
(success, response) = address(callAddress).call{value: gi.value}(gi.callData);
```

There is no `nonReentrant` modifier on `execute()`. If `callAddress` (or any contract in the call chain) calls back into `execute()` with a higher-sequence VAA during its execution, that VAA executes successfully and advances `lastExecutedSequence` to the higher value. When the outer call returns, any intermediate sequence numbers are permanently unexecutable — they fail the `vm.sequence <= lastExecutedSequence` check with `MessageOutOfOrder`.

Attack flow (governance has issued VAAs seq=5, seq=6, seq=7, all publicly available on Wormhole):

1. Attacker calls `execute(VAA_seq_5)` — `lastExecutedSequence = 5`, external call to `callAddress` begins
2. `callAddress` (or a contract it calls) calls `execute(VAA_seq_7)` — `lastExecutedSequence = 7`, VAA_seq_7 executes
3. Outer call for VAA_seq_5 completes normally
4. `execute(VAA_seq_6)` now permanently reverts with `MessageOutOfOrder` (6 ≤ 7)

VAA_seq_6 is irreversibly skipped.

### Impact Explanation

A governance instruction (e.g., a critical parameter change, data source update, or contract upgrade) can be permanently blocked and never executed. The `Executor` is used for cross-chain governance actions; skipping a VAA in the sequence means that governance action is lost with no recovery path, since `lastExecutedSequence` only moves forward.

### Likelihood Explanation

`execute()` is `public payable` — any unprivileged caller can submit a valid governance VAA. The attack requires `callAddress` to reenter `execute()` with a higher-sequence VAA. `callAddress` is governance-specified, but governance legitimately calls contracts that may have `receive()` hooks or callback patterns (e.g., treasury contracts, multisigs, DeFi protocols). If multiple governance VAAs are in-flight simultaneously (a common pattern for multi-step upgrades), all are publicly available on Wormhole and can be submitted by anyone. The window exists whenever governance issues sequential VAAs where one calls a contract with any ETH-receive or callback logic.

### Recommendation

Add a `nonReentrant` modifier (OpenZeppelin `ReentrancyGuard`) to `execute()`:

```solidity
function execute(bytes memory encodedVm) public payable nonReentrant returns (bytes memory response) {
```

This directly mirrors the recommended fix in the Axelar report and eliminates the reordering/skipping attack vector entirely.

### Proof of Concept

```
State: lastExecutedSequence = 4
Governance has issued VAA_seq_5 (calls ContractA), VAA_seq_6 (SetFee), VAA_seq_7 (calls ContractB)
All three VAAs are publicly available on Wormhole.

ContractA has a receive() that calls executor.execute(VAA_seq_7).

1. Attacker calls executor.execute(VAA_seq_5)
   → verifyGovernanceVM: 5 > 4 ✓, lastExecutedSequence = 5
   → external call to ContractA begins

2. ContractA.receive() calls executor.execute(VAA_seq_7)
   → verifyGovernanceVM: 7 > 5 ✓, lastExecutedSequence = 7
   → external call to ContractB, completes
   → execute(VAA_seq_7) returns

3. ContractA returns, execute(VAA_seq_5) completes

4. executor.execute(VAA_seq_6) called:
   → verifyGovernanceVM: 6 <= 7 → revert MessageOutOfOrder
   → VAA_seq_6 is permanently unexecutable
```

Relevant code: [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L66-94)
```text
    function execute(
        bytes memory encodedVm
    ) public payable returns (bytes memory response) {
        IWormhole.VM memory vm = verifyGovernanceVM(encodedVm);

        GovernanceInstruction memory gi = parseGovernanceInstruction(
            vm.payload
        );

        if (gi.targetChainId != chainId && gi.targetChainId != 0)
            revert ExecutorErrors.InvalidGovernanceTarget();

        if (
            gi.action != ExecutorAction.Execute ||
            gi.executorAddress != address(this)
        ) revert ExecutorErrors.DeserializationError();

        // Check if the gi.callAddress is a contract account.
        uint len;
        address callAddress = address(gi.callAddress);
        assembly {
            len := extcodesize(callAddress)
        }
        if (len == 0) revert ExecutorErrors.InvalidContractTarget();

        bool success;
        (success, response) = address(callAddress).call{value: gi.value}(
            gi.callData
        );
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L117-137)
```text
    function verifyGovernanceVM(
        bytes memory encodedVM
    ) internal returns (IWormhole.VM memory parsedVM) {
        (IWormhole.VM memory vm, bool valid, ) = wormhole.parseAndVerifyVM(
            encodedVM
        );

        if (!valid) revert ExecutorErrors.InvalidWormholeVaa();

        if (
            vm.emitterChainId != ownerEmitterChainId ||
            vm.emitterAddress != ownerEmitterAddress
        ) revert ExecutorErrors.UnauthorizedEmitter();

        if (vm.sequence <= lastExecutedSequence)
            revert ExecutorErrors.MessageOutOfOrder();

        lastExecutedSequence = vm.sequence;

        return vm;
    }
```

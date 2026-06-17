### Title
Governance Sequence Skipping via Out-of-Order VAA Submission — (`target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol`)

---

### Summary

`verifyGovernanceVM` enforces only `vm.sequence > lastExecutedGovernanceSequence()` (a "greater-than" check, not "equals next expected"). Because `executeGovernanceInstruction` is `public` and Wormhole VAAs are publicly observable, any unprivileged caller can submit a higher-sequence governance VAA before a lower-sequence one has been processed, permanently making the lower-sequence instruction unexecutable.

---

### Finding Description

The guard in `verifyGovernanceVM` is:

```solidity
if (vm.sequence <= lastExecutedGovernanceSequence())
    revert PythErrors.OldGovernanceMessage();

setLastExecutedGovernanceSequence(vm.sequence);
``` [1](#0-0) 

The entry point is unconditionally public:

```solidity
function executeGovernanceInstruction(bytes calldata encodedVM) public {
    IWormhole.VM memory vm = verifyGovernanceVM(encodedVM);
``` [2](#0-1) 

**Attack path (concrete):**

1. Governance emits seq=S and seq=S+1, both targeting chain X (or `targetChainId=0`). Both VAAs are signed by Wormhole guardians and become publicly observable on the Wormhole network.
2. Attacker fetches the seq=S+1 VAA bytes from the Wormhole guardian network before the legitimate relayer submits seq=S.
3. Attacker calls `executeGovernanceInstruction(vaa_S_plus_1)` with a higher gas price (MEV front-run).
4. `verifyGovernanceVM` checks `S+1 > lastExecutedGovernanceSequence()` → passes. `setLastExecutedGovernanceSequence(S+1)` is written to state.
5. Any subsequent attempt to submit seq=S fails: `S <= S+1` → `revert OldGovernanceMessage`. seq=S is permanently unexecutable.

The `targetChainId` check at line 71 occurs **after** `verifyGovernanceVM` has already committed the sequence update, but since a wrong-chain VAA would revert the whole transaction, the attacker only needs a VAA that is valid for the current chain — which is exactly the precondition stated. [3](#0-2) 

---

### Impact Explanation

A voted governance instruction (seq=S) is permanently dropped and can never be executed. Depending on what seq=S contained, this could mean:

- A `SetDataSources` change (removing a compromised price source) is silently skipped.
- A `UpgradeContract` or `SetWormholeAddress` instruction is skipped.
- Any other governance action approved by the governance body is nullified.

This matches the stated scope: **governance voting result manipulation — the voted outcome is changed away from what was approved**.

---

### Likelihood Explanation

- `executeGovernanceInstruction` is `public` with no access control.
- Wormhole VAAs are publicly available from guardian APIs immediately after signing.
- On Ethereum, MEV infrastructure (Flashbots, etc.) makes front-running governance relay transactions straightforward.
- The attacker does not need any privileged role, key, or stake — only the ability to call a public function with a valid VAA obtained from a public API.

---

### Recommendation

Replace the `>` check with a strict consecutive-sequence check:

```solidity
if (vm.sequence != lastExecutedGovernanceSequence() + 1)
    revert PythErrors.InvalidGovernanceSequence();
```

If intentional gaps are required for cross-chain governance (some sequences target other chains), the governance relayer should enforce ordering off-chain, and the contract should at minimum document and accept the gap-skipping risk. Alternatively, require that the submitter be a whitelisted relayer address, or use a commit-reveal / timelock pattern so that seq=S must be committed before seq=S+1 can be finalized.

---

### Proof of Concept

State-transition test (Hardhat/Foundry pseudocode):

```solidity
// Setup: lastExecutedGovernanceSequence = 0
// vaa_1 = valid governance VAA with sequence=1, targetChainId=currentChain
// vaa_2 = valid governance VAA with sequence=2, targetChainId=currentChain

// Attacker submits seq=2 first
pyth.executeGovernanceInstruction(vaa_2);
// lastExecutedGovernanceSequence is now 2

// Legitimate relayer tries to submit seq=1
vm.expectRevert(PythErrors.OldGovernanceMessage.selector);
pyth.executeGovernanceInstruction(vaa_1);
// seq=1 instruction is permanently skipped
``` [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol (L44-62)
```text
    function verifyGovernanceVM(
        bytes memory encodedVM
    ) internal returns (IWormhole.VM memory parsedVM) {
        (IWormhole.VM memory vm, bool valid, ) = wormhole().parseAndVerifyVM(
            encodedVM
        );

        if (!valid) revert PythErrors.InvalidWormholeVaa();

        if (!isValidGovernanceDataSource(vm.emitterChainId, vm.emitterAddress))
            revert PythErrors.InvalidGovernanceDataSource();

        if (vm.sequence <= lastExecutedGovernanceSequence())
            revert PythErrors.OldGovernanceMessage();

        setLastExecutedGovernanceSequence(vm.sequence);

        return vm;
    }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol (L64-65)
```text
    function executeGovernanceInstruction(bytes calldata encodedVM) public {
        IWormhole.VM memory vm = verifyGovernanceVM(encodedVM);
```

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol (L71-72)
```text
        if (gi.targetChainId != chainId() && gi.targetChainId != 0)
            revert PythErrors.InvalidGovernanceTarget();
```

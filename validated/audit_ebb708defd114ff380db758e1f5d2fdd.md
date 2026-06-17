### Title
Unguarded `_entropyCallback` Invocation in Legacy `revealWithCallback` Path Enables Permanent Request DoS — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function contains two execution paths. The **new path** (when `req.gasLimit10k != 0`) correctly uses `excessivelySafeCall` to catch callback reverts and transitions the request to a `CALLBACK_FAILED` recoverable state. However, the **legacy path** (when `req.gasLimit10k == 0`, i.e., the provider has not set a `defaultGasLimit`) invokes `IEntropyConsumer(callAddress)._entropyCallback(...)` as a bare, unchecked external call with no try/catch and no error-handling state machine. If the requester's contract reverts in `_entropyCallback`, the entire `revealWithCallback` transaction reverts, and the request is permanently stuck — it can never be fulfilled.

---

### Finding Description

In `revealWithCallback`, when `req.gasLimit10k == 0` (the legacy path), the code clears the request from storage and then calls the requester's callback without any error protection:

```solidity
clearRequest(provider, sequenceNumber);
// ...
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    );
}
``` [1](#0-0) 

Because `clearRequest` is called before the callback, and the callback is not wrapped in try/catch, a revert in `_entropyCallback` causes the entire transaction to revert — including the `clearRequest`. The request is restored to `CALLBACK_NOT_STARTED` state. Since there is no `CALLBACK_FAILED` recovery state for this path, and the callback will always revert for a malicious requester, the request is permanently stuck and can never be fulfilled.

The new path (gasLimit10k != 0) correctly handles this with `excessivelySafeCall`:

```solidity
(success, ret) = req.requester.excessivelySafeCall(
    uint256(req.gasLimit10k) * TEN_THOUSAND,
    256,
    abi.encodeWithSelector(IEntropyConsumer._entropyCallback.selector, ...)
);
``` [2](#0-1) 

The legacy path is still reachable. When a provider has `defaultGasLimit == 0`, all requests to that provider set `req.gasLimit10k = 0`:

```solidity
if (providerInfo.defaultGasLimit == 0) {
    req.gasLimit10k = 0;
}
``` [3](#0-2) 

The fee is credited to the provider immediately at request time in `requestHelper`, before any callback:

```solidity
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [4](#0-3) 

There is no refund mechanism for stuck requests. The user's fee is permanently locked in the provider's accrued balance.

---

### Impact Explanation

- A malicious requester deploys a contract that always reverts in `_entropyCallback`. Every provider attempt to call `revealWithCallback` reverts. The request is permanently stuck.
- The user's fee (paid at request time) is irrecoverably locked in the provider's `accruedFeesInWei` — the user paid for a service they can never receive.
- The provider's sequence number is consumed and cannot be reused, degrading the provider's capacity.
- There is no admin escape hatch, no `CALLBACK_FAILED` recovery state, and no refund path for the legacy code branch.

---

### Likelihood Explanation

Any unprivileged user can trigger this by:
1. Identifying a provider with `defaultGasLimit == 0` (providers that have not opted into the new callback failure flow).
2. Deploying a contract whose `_entropyCallback` unconditionally reverts.
3. Calling `requestWithCallback` or `requestV2(..., 0)` from that contract, paying only the standard fee.

The `requestWithCallback` function is the deprecated but still-live entry point that routes through this path:

```solidity
function requestWithCallback(address provider, bytes32 userContribution)
    public payable override returns (uint64) {
    return requestV2(provider, userContribution, 0);
}
``` [5](#0-4) 

The `IEntropyConsumer` interface documents that `_entropyCallback` must never revert, but this is a consumer-side advisory — the contract itself does not enforce it in the legacy path: [6](#0-5) 

---

### Recommendation

Apply the same `excessivelySafeCall` + `CALLBACK_FAILED` state machine used in the new path to the legacy path as well. Specifically, wrap the bare `_entropyCallback` invocation in the `else` branch with a try/catch or `excessivelySafeCall`, and on failure emit a `CallbackFailed` event without reverting the outer transaction. This mirrors the fix already applied to the `gasLimit10k != 0` path and is the direct analog of the ERC721 `safeTransferFrom` + try/catch recommendation in the reference report.

---

### Proof of Concept

```solidity
// Malicious requester contract
contract MaliciousRequester is IEntropyConsumer {
    IEntropy entropy;
    constructor(address _entropy) { entropy = IEntropy(_entropy); }

    function attack(address provider, bytes32 userCommitment) external payable {
        // provider must have defaultGasLimit == 0 to trigger legacy path
        entropy.requestWithCallback{value: msg.value}(provider, userCommitment);
    }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }

    function entropyCallback(uint64, address, bytes32) internal override {
        revert("always revert"); // permanently blocks revealWithCallback
    }
}
```

1. Deploy `MaliciousRequester` pointing at the Entropy contract.
2. Call `attack(provider, userCommitment)` with sufficient fee. Fee is immediately credited to provider.
3. Provider calls `revealWithCallback(provider, seq, userContrib, providerContrib)` — transaction reverts every time.
4. Request is permanently stuck. User's fee is locked. Provider's sequence number is consumed.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L237-239)
```text
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-272)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L582-596)
```text
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

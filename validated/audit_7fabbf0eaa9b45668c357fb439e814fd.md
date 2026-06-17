### Title
Malicious Requester Can Permanently Block `revealWithCallback` for Legacy Providers - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary
In `Entropy.sol`, the `revealWithCallback` function contains a legacy code path (entered when `req.gasLimit10k == 0`) that calls `_entropyCallback` on the requester contract **without any error handling**. A malicious requester can deploy a contract whose `_entropyCallback` always reverts, permanently preventing the request from ever being fulfilled and causing the provider's keeper service to waste gas indefinitely.

---

### Finding Description

`revealWithCallback` in `Entropy.sol` branches on whether the request has a gas limit set:

**New (safe) path** — entered when `req.gasLimit10k != 0 && callbackStatus == CALLBACK_NOT_STARTED`:
Uses `excessivelySafeCall`, which catches reverts and transitions the request to `CALLBACK_FAILED` state without reverting the outer transaction. [1](#0-0) 

**Legacy (unsafe) path** — the `else` branch, entered when `req.gasLimit10k == 0`:
Calls `_entropyCallback` directly with no try/catch: [2](#0-1) 

`req.gasLimit10k` is set to `0` for all requests made to a provider whose `defaultGasLimit == 0`: [3](#0-2) 

In the legacy path, `clearRequest` is called **before** the callback (line 666), but because the callback revert propagates upward and reverts the entire transaction, `clearRequest` is also undone. The request remains permanently stuck — it can never be cleared, and `revealWithCallback` will always revert when called for it.

This behavior is explicitly confirmed by the existing test: [4](#0-3) 

The fixed-size primary request table has only `NUM_REQUESTS = 32` slots: [5](#0-4) [6](#0-5) 

Overflow requests go to an unbounded `requestsOverflow` mapping, so the 32-slot primary table is not the binding constraint. However, each stuck request permanently consumes a provider sequence number from the provider's finite hash chain (`endSequenceNumber`), and the Fortuna keeper service will retry the reveal indefinitely, burning gas on every attempt.

---

### Impact Explanation

- **Provider keeper griefing**: The Fortuna keeper (`apps/fortuna`) continuously polls for unfulfilled requests and calls `revealWithCallback`. Every retry against a stuck request reverts and wastes gas, with no recovery path.
- **Sequence number exhaustion**: Each stuck request permanently consumes one slot in the provider's finite hash chain. A sustained attack can exhaust `endSequenceNumber`, forcing the provider to re-register.
- **Permanent request lock**: There is no admin escape hatch or timeout mechanism to forcibly clear a stuck legacy-path request.

---

### Likelihood Explanation

- Any unprivileged user can deploy a contract that reverts in `_entropyCallback` and call `requestWithCallback` against any provider with `defaultGasLimit == 0`.
- The cost is only the request fee (paid at request time, already credited to the provider at line 237).
- The attack is fully permissionless, requires no privileged access, and is irreversible once submitted. [7](#0-6) 

---

### Recommendation

Wrap the direct `_entropyCallback` call in the legacy path with a `try/catch` (or use `excessivelySafeCall`), so that a reverting callback does not revert the outer transaction. The request should be cleared regardless of whether the callback succeeds, consistent with the behavior of the new path. For example:

```solidity
// Instead of:
IEntropyConsumer(callAddress)._entropyCallback(sequenceNumber, provider, randomNumber);

// Use:
try IEntropyConsumer(callAddress)._entropyCallback(sequenceNumber, provider, randomNumber) {
    // success
} catch {
    emit CallbackFailed(...);
}
```

---

### Proof of Concept

1. Deploy a malicious contract `MaliciousConsumer` that implements `_entropyCallback` with `revert("griefed")`.
2. Call `requestWithCallback(provider, userContribution)` against a provider whose `defaultGasLimit == 0`. This sets `req.gasLimit10k = 0` on the stored request.
3. The provider's Fortuna keeper calls `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
4. Execution reaches line 676: `IEntropyConsumer(callAddress)._entropyCallback(...)` — this reverts.
5. The entire transaction reverts. `clearRequest` is undone. The request remains active.
6. Every subsequent keeper retry also reverts. The sequence number is permanently consumed and the keeper burns gas on every block. [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L234-238)
```text
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
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

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L999-1017)
```text
    function testRequestWithCallbackAndRevealWithCallbackFailing() public {
        bytes32 userRandomNumber = bytes32(uint(42));
        uint fee = random.getFee(provider1);
        EntropyConsumer consumer = new EntropyConsumer(address(random), true);
        vm.deal(address(consumer), fee);
        vm.startPrank(address(consumer));
        uint64 assignedSequenceNumber = random.requestWithCallback{value: fee}(
            provider1,
            userRandomNumber
        );

        vm.expectRevert();
        random.revealWithCallback(
            provider1,
            assignedSequenceNumber,
            userRandomNumber,
            provider1Proofs[assignedSequenceNumber]
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L33-34)
```text
        EntropyStructsV2.Request[32] requests;
        mapping(bytes32 => EntropyStructsV2.Request) requestsOverflow;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L47-48)
```text
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
```

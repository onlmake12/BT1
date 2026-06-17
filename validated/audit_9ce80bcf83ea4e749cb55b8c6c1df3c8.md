### Title
Legacy `revealWithCallback` Path Permanently Locks User Entropy Requests When Callback Reverts - (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

When a user requests entropy via `requestWithCallback` (or `requestV2` with `gasLimit=0`) against a provider whose `defaultGasLimit` is unset (zero), the resulting request uses a legacy fulfillment path in `revealWithCallback`. In this path, if the requester contract's `_entropyCallback` reverts for any reason, the entire `revealWithCallback` transaction reverts — including the `clearRequest` state change — leaving the request permanently stuck in storage with no recovery mechanism. The user's fee is permanently locked and the random number is never delivered.

---

### Finding Description

`Entropy.sol` contains two distinct fulfillment paths inside `revealWithCallback`:

**New path** (`req.gasLimit10k != 0`): Uses `excessivelySafeCall` to isolate callback failures. A reverting callback transitions the request to `CALLBACK_FAILED` state, from which it can be retried.

**Legacy path** (`req.gasLimit10k == 0`): Calls the requester's `_entropyCallback` directly with no error isolation:

```solidity
} else {
    address callAddress = req.requester;
    EntropyStructs.Request memory reqV1 = EntropyStructConverter.toV1Request(req);
    clearRequest(provider, sequenceNumber);
    // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

    if (len != 0) {
        IEntropyConsumer(callAddress)._entropyCallback(
            sequenceNumber,
            provider,
            randomNumber
        );
    }
``` [1](#0-0) 

`clearRequest` is called **before** the callback. If `_entropyCallback` reverts, the entire transaction reverts — including `clearRequest` — so the request is restored to storage. Every subsequent call to `revealWithCallback` for this sequence number will also revert. There is no `CALLBACK_FAILED` state, no retry mechanism, and no cancel/refund function.

The legacy path is triggered when `req.gasLimit10k == 0`. This occurs when `requestWithCallback` is used with a provider whose `defaultGasLimit` is zero (the default for newly registered providers):

```solidity
function requestWithCallback(
    address provider,
    bytes32 userContribution
) public payable override returns (uint64) {
    return requestV2(provider, userContribution, 0);
}
``` [2](#0-1) 

The existing test `testRequestWithCallbackAndRevealWithCallbackFailing` explicitly confirms this behavior — `revealWithCallback` reverts when the callback reverts in the legacy path: [3](#0-2) 

---

### Impact Explanation

- The user's entropy request is permanently stuck in storage and can never be fulfilled.
- The user's fee (paid at request time and accrued to the provider) is permanently locked with no refund path.
- The random number is never delivered to the requester contract.
- Unlike the new `gasLimit10k != 0` path — which has `CALLBACK_FAILED` state and a recovery flow — the legacy path has **no recovery mechanism whatsoever**.

The impact is analogous to M-16: an external dependency (the requester contract's callback behavior) causes permanent loss of functionality with no administrative escape hatch.

---

### Likelihood Explanation

- Any provider that has not called `setDefaultGasLimit` has `defaultGasLimit = 0`, which is the default state for all newly registered providers.
- Any user calling `requestWithCallback` (the documented, non-V2 API) against such a provider lands in the legacy path.
- Callback reverts are realistic: the requester contract may be upgraded to remove or change `_entropyCallback`, its implementation may be self-destructed (proxy pattern), or it may contain a logic bug that causes reversion under certain conditions.
- The Pyth documentation itself warns: *"This method should never return an error — if it returns an error, then the keeper will not be able to invoke the callback."* This warning acknowledges the scenario is realistic. [4](#0-3) 

---

### Recommendation

Apply the same `excessivelySafeCall` + `CALLBACK_FAILED` state machine to the legacy path (`gasLimit10k == 0`). When the callback reverts in the legacy path, transition the request to `CALLBACK_FAILED` instead of reverting the entire transaction. This mirrors the recovery design already present in the new path: [5](#0-4) 

Alternatively, add a `cancelRequest` function that allows the original requester to cancel a stuck request and reclaim their fee after a timeout period.

---

### Proof of Concept

1. Deploy a provider with `defaultGasLimit = 0` (default state — no `setDefaultGasLimit` call needed).
2. Deploy a requester contract whose `_entropyCallback` always reverts (e.g., `revert("Callback failed")`).
3. Call `requestWithCallback(provider, userRandomNumber)` from the requester contract, paying the fee.
4. The provider calls `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
5. The transaction reverts because `_entropyCallback` reverts and there is no error isolation.
6. The request remains in storage. Repeat step 4 — it reverts every time.
7. The user's fee is permanently locked. The request can never be cleared. There is no cancel or refund function.

This is confirmed by the existing test at: [3](#0-2)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L568-600)
```text
        // If the request has an explicit gas limit, then run the new callback failure state flow.
        //
        // Requests that haven't been invoked yet will be invoked safely (catching reverts), and
        // any reverts will be reported as an event. Any failing requests move to a failure state
        // at which point they can be recovered. The recovery flow invokes the callback directly
        // (no catching errors) which allows callers to easily see the revert reason.
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

**File:** apps/developer-hub/content/docs/entropy/generate-random-numbers-evm.mdx (L124-127)
```text
    // This method **must** be implemented on the same contract that requested the random number.
    // This method should **never** return an error -- if it returns an error, then the keeper will not be able to invoke the callback.
    // If you are having problems receiving the callback, the most likely cause is that the callback is erroring.
    // See the callback debugging guide here to identify the error https://docs.pyth.network/entropy/debug-callback-failures
```

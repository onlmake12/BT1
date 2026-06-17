### Title
Legacy Entropy Callback Path Makes Unprotected Direct Call, Permanently Locking Fees on Reverting Callbacks — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.sol`'s `revealWithCallback` function contains two code paths for invoking the user's `_entropyCallback`. The modern path (when `req.gasLimit10k != 0`) uses `excessivelySafeCall` to gracefully catch reverts. The legacy path (when `req.gasLimit10k == 0`, i.e., the provider has never set a `defaultGasLimit`) makes a **bare, unprotected direct call** to the requester's callback. If that callback reverts for any reason, the entire `revealWithCallback` transaction reverts — including the `clearRequest` effect that preceded it — permanently trapping the user's fee and making the request unfulfillable.

---

### Finding Description

In `requestHelper`, the `gasLimit10k` field is set to `0` whenever the provider's `defaultGasLimit` is `0`:

```solidity
if (providerInfo.defaultGasLimit == 0) {
    // Provider doesn't support the new callback failure state flow
    req.gasLimit10k = 0;
}
``` [1](#0-0) 

When `revealWithCallback` is later called for such a request, the branching condition routes execution into the legacy `else` block:

```solidity
if (
    req.gasLimit10k != 0 &&
    req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
) {
    // ... excessivelySafeCall path ...
} else {
    address callAddress = req.requester;
    ...
    clearRequest(provider, sequenceNumber);
    // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED
    ...
    if (len != 0) {
        IEntropyConsumer(callAddress)._entropyCallback(
            sequenceNumber,
            provider,
            randomNumber
        );
    }
``` [2](#0-1) 

The direct call at line 676 has **no try/catch and no `excessivelySafeCall` wrapper**. If `_entropyCallback` reverts for any reason (logic error, out-of-gas, intentional revert), the EVM unwinds the entire transaction, including the `clearRequest` call at line 666. The request is therefore never cleared and the user's fee remains locked in the contract with no recovery path.

The modern path, by contrast, uses `excessivelySafeCall` and transitions the request to `CALLBACK_FAILED` state on revert, allowing the provider to retry: [3](#0-2) 

The legacy path has no equivalent failure state and no retry mechanism.

The `requestWithCallback` legacy entry point explicitly passes `gasLimit = 0`, which will always produce `gasLimit10k = 0` for any provider whose `defaultGasLimit` is `0`: [4](#0-3) 

---

### Impact Explanation

For any provider whose `defaultGasLimit` is `0`:

- A user whose `entropyCallback` reverts (due to a bug, an `assert`/`require` failure, or deliberate construction) causes `revealWithCallback` to revert entirely.
- Because `clearRequest` is called before the callback (checks-effects-interactions pattern), the revert rolls back the storage clear, leaving the request permanently active.
- The user's fee is permanently locked in the contract; there is no refund function.
- The provider can never earn the fee for that sequence number; every retry attempt reverts identically.
- A malicious user can deliberately deploy a reverting callback, make many requests against a legacy provider, and render the provider unable to fulfill any of those sequence numbers — a targeted, low-cost griefing attack against the provider's operational continuity.

---

### Likelihood Explanation

- The `requestWithCallback` function (the original V1-style API) always passes `gasLimit = 0`, so every request made through it against a provider with `defaultGasLimit == 0` is affected.
- Providers that have not yet called `setDefaultGasLimit` retain `defaultGasLimit == 0` and are fully exposed.
- Callback reverts are common in practice: the official documentation explicitly warns that callbacks must never revert and provides a dedicated debugging guide for this exact failure mode, confirming it is a frequent real-world occurrence. [5](#0-4) 

---

### Recommendation

Wrap the direct callback invocation in the legacy `else` branch with a `try/catch` (or `excessivelySafeCall`) and introduce a `CALLBACK_FAILED` state transition analogous to the modern path. This allows the provider to report the failure without reverting the entire transaction and prevents permanent fee lockup:

```solidity
} else {
    address callAddress = req.requester;
    clearRequest(provider, sequenceNumber);
    if (len != 0) {
        try IEntropyConsumer(callAddress)._entropyCallback(
            sequenceNumber, provider, randomNumber
        ) {
            // success
        } catch {
            // emit failure event; do not revert
        }
    }
}
```

Alternatively, require all providers to set a non-zero `defaultGasLimit` before accepting new requests, eliminating the legacy path entirely.

---

### Proof of Concept

1. Deploy a provider with `defaultGasLimit == 0` (never called `setDefaultGasLimit`).
2. Deploy a consumer contract whose `entropyCallback` always reverts:
   ```solidity
   function entropyCallback(uint64, address, bytes32) internal override {
       revert("always fails");
   }
   ```
3. Call `requestWithCallback(provider, userContribution)` — this stores `gasLimit10k = 0`.
4. Call `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
5. Observe: the transaction reverts with `"always fails"`.
6. Observe: `getRequestV2(provider, sequenceNumber)` still returns the active request (not cleared).
7. Every subsequent `revealWithCallback` attempt reverts identically.
8. The user's fee is permanently locked; the provider can never fulfill this sequence number.

The existing test `testRequestWithRevertingCallback` in `Entropy.t.sol` confirms this revert behavior for the legacy path: [6](#0-5)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-681)
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

            if (success) {
                emit RevealedWithCallback(
                    EntropyStructConverter.toV1Request(req),
                    userContribution,
                    providerContribution,
                    randomNumber
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    req.sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    false,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                clearRequest(provider, sequenceNumber);
            } else if (
                (startingGas * 31) / 32 >
                uint256(req.gasLimit10k) * TEN_THOUSAND
            ) {
                // The callback reverted for some reason.
                // We don't use ret to condition the behavior here (out-of-gas or other revert), as we have found that some user contracts
                // catch out-of-gas errors and revert with a different error.
                // In this case, ensure that the callback was provided with sufficient gas. Technically, 63/64ths of the startingGas is forwarded,
                // but we're using 31/32 to introduce a margin of safety.
                emit CallbackFailed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    userContribution,
                    providerContribution,
                    randomNumber,
                    ret
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    true,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED;
            } else {
                // Callback reverted by (potentially) running out of gas, but the calling context did not have enough gas
                // to run the callback. This is a corner case that can happen due to the nuances of gas passing
                // in calls (see the comment on the call above).
                //
                // (Note that reverting here plays nicely with the estimateGas RPC method, which binary searches for
                // the smallest gas value that causes the transaction to *succeed*. See https://github.com/ethereum/go-ethereum/pull/3587 )
                revert EntropyErrors.InsufficientGas();
            }
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

**File:** apps/developer-hub/content/docs/entropy/generate-random-numbers-evm.mdx (L150-155)
```text
<Callout type="warning">
  The `entropyCallback` function on your contract should **never** return an
  error. If it returns an error, the keeper will not be able to invoke the
  callback. If you are having problems receiving the callback, please see
  [Debugging Callback Failures](./debug-callback-failures).
</Callout>
```

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L1102-1124)
```text
    function testRequestWithRevertingCallback() public {
        uint32 defaultGasLimit = 100000;
        vm.prank(provider1);
        random.setDefaultGasLimit(defaultGasLimit);

        bytes32 userRandomNumber = bytes32(uint(42));
        uint fee = random.getFee(provider1);
        EntropyConsumer consumer = new EntropyConsumer(address(random), true);
        vm.deal(user1, fee);
        vm.prank(user1);
        uint64 assignedSequenceNumber = consumer.requestEntropy{value: fee}(
            userRandomNumber
        );

        // If the callback reverts, the Entropy reveal also reverts unless
        // provided enough gas to pass on.
        vm.expectRevert();
        random.revealWithCallback{gas: defaultGasLimit - 1000}(
            provider1,
            assignedSequenceNumber,
            userRandomNumber,
            provider1Proofs[assignedSequenceNumber]
        );
```

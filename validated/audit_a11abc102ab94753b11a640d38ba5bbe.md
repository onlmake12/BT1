### Title
Reverting `_entropyCallback` in Legacy Path Permanently Blocks `revealWithCallback` — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

In `Entropy.sol`, the `revealWithCallback` function has two execution paths. When a provider has `defaultGasLimit == 0` (the legacy/opt-out mode), the `else` branch is taken, which calls `IEntropyConsumer(callAddress)._entropyCallback(...)` directly with no revert protection. A malicious requester can deploy a contract whose `_entropyCallback` always reverts, permanently preventing their request from ever being fulfilled and causing any caller of `revealWithCallback` for that sequence number to have their transaction revert.

### Finding Description

In `requestHelper`, the `gasLimit10k` field of a stored request is set to `0` whenever the provider's `defaultGasLimit` is `0`: [1](#0-0) 

In `revealWithCallback`, the branch condition at line 574 routes to the safe path only when `req.gasLimit10k != 0 && callbackStatus == CALLBACK_NOT_STARTED`. All other cases — including `gasLimit10k == 0` (legacy provider) — fall into the `else` branch: [2](#0-1) 

In this `else` branch, `clearRequest` is called first (line 666), then `_entropyCallback` is called directly with no `try/catch` or `excessivelySafeCall` wrapper (line 676). If `_entropyCallback` reverts, the entire transaction reverts — including the `clearRequest` — leaving the request permanently active and unfulfillable.

The safe path (lines 574–660) uses `excessivelySafeCall` to catch reverts and transitions the request to `CALLBACK_FAILED` state, allowing recovery. The legacy path has no such protection.

This is confirmed by the existing test `testRequestWithCallbackAndRevealWithCallbackFailing`: [3](#0-2) 

The test explicitly shows `vm.expectRevert()` when a consumer with a reverting callback is used against a provider with no default gas limit set.

### Impact Explanation

- Any call to `revealWithCallback` for the stuck request permanently reverts. Since `revealWithCallback` is callable by anyone (provider, relayer, or third party), the provider's automated fulfillment service will have its transactions revert and waste gas.
- The request occupies a slot in the provider's sequence number space indefinitely, contributing to storage bloat.
- The request can never be cleared, meaning the provider's `numHashes` computation for future requests against the same provider grows unboundedly relative to this stuck sequence number.
- The requester's paid fee is effectively burned (provider keeps it, but no service is rendered).

### Likelihood Explanation

Any provider that has not called `setDefaultGasLimit` (i.e., `defaultGasLimit == 0`) is in the legacy path. A malicious or buggy requester contract with a reverting `_entropyCallback` triggers this. The entry point is fully permissionless — `requestWithCallback` is public and payable. The attacker only needs to pay the provider's fee. [4](#0-3) 

### Recommendation

Apply `excessivelySafeCall` (already imported and used in the safe path) to the legacy `else` branch as well, or transition the legacy path to also use a `CALLBACK_FAILED` state on revert. Alternatively, deprecate the `gasLimit10k == 0` path entirely and require all providers to set a non-zero `defaultGasLimit` before accepting `requestWithCallback` calls.

### Proof of Concept

1. Deploy a provider with `defaultGasLimit == 0` (never calls `setDefaultGasLimit`).
2. Deploy a malicious requester contract whose `_entropyCallback` always executes `revert("griefed")`.
3. Call `requestWithCallback{value: fee}(provider, userContribution)` from the malicious contract.
4. Attempt `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)` from any address.
5. Observe the transaction reverts unconditionally. The request remains active in storage forever.

The existing test at line 999–1017 of `Entropy.t.sol` already demonstrates steps 3–5 with `vm.expectRevert()`. [5](#0-4)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-702)
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
            uint32 gasUsed = SafeCast.toUint32(startingGas - gasleft());

            emit RevealedWithCallback(
                reqV1,
                userContribution,
                providerContribution,
                randomNumber
            );
            emit EntropyEventsV2.Revealed(
                provider,
                callAddress,
                sequenceNumber,
                randomNumber,
                userContribution,
                providerContribution,
                false,
                bytes(""),
                gasUsed,
                bytes("")
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

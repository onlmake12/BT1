### Title
Silent Callback Success on Non-Existent Requester in New Gas-Limit Flow — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

`Entropy.revealWithCallback` contains two distinct code paths for delivering the callback. The legacy path (when `req.gasLimit10k == 0`) explicitly checks `extcodesize` before calling the requester. The newer path (when `req.gasLimit10k != 0`) uses `excessivelySafeCall` with no contract-existence check. Because `call` (and therefore `excessivelySafeCall`) returns `(true, [])` for any non-existent account per EVM design, a callback to an EOA or a self-destructed contract silently reports success, permanently clears the request, and consumes the consumer's fee — all without ever executing the callback.

---

### Finding Description

**Old flow** (`gasLimit10k == 0`, lines 669–681): an explicit `extcodesize` guard is present before invoking `_entropyCallback`. If the requester has no code, the call is simply skipped. [1](#0-0) 

**New flow** (`gasLimit10k != 0`, lines 574–660): `excessivelySafeCall` is invoked directly on `req.requester` with no prior existence check. [2](#0-1) 

`excessivelySafeCall` wraps a raw `call` opcode. Per the Solidity documentation (and EVM spec), `call` to a non-existent account returns `1` (success) with empty return data. Therefore, when `req.requester` is an EOA or a self-destructed contract:

1. `excessivelySafeCall` returns `(true, [])`.
2. The `if (success)` branch is taken.
3. `clearRequest` permanently deletes the in-flight request.
4. `RevealedWithCallback` is emitted with `failed = false`.
5. The consumer's random number is irrecoverably lost; no `CALLBACK_FAILED` state is set, so no retry is possible. [3](#0-2) 

The inconsistency is explicit: the old flow guards with `extcodesize`; the new flow does not. [4](#0-3) 

---

### Impact Explanation

- The consumer's request is permanently cleared and their paid fee is consumed.
- The random number is generated but never delivered; there is no retry path because the request is not moved to `CALLBACK_FAILED`.
- Off-chain monitoring and integrators observe a `RevealedWithCallback` success event, masking the delivery failure entirely.
- Scope: loss of paid service / permanent loss of the consumer's in-flight request funds.

---

### Likelihood Explanation

Two realistic triggers exist:

1. **EOA caller with gasLimit > 0**: Any EOA can call `requestV2(provider, userContribution, gasLimit)` with a non-zero `gasLimit`. The protocol stores `msg.sender` (an EOA) as `req.requester`. On reveal, the callback silently succeeds.
2. **Self-destructed consumer contract**: A consumer contract makes a request, is subsequently self-destructed (upgrade bug, intentional teardown), and the provider later calls `revealWithCallback`. The callback silently succeeds.

Both paths are reachable by an unprivileged actor without any privileged role. [5](#0-4) 

---

### Recommendation

Apply the same `extcodesize` guard used in the old flow before the `excessivelySafeCall` in the new flow:

```solidity
uint256 codeSize;
assembly { codeSize := extcodesize(callAddress) }
if (codeSize == 0) {
    // Emit a failure event or revert; do not silently succeed.
    req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED;
    emit CallbackFailed(...);
    return;
}
(success, ret) = req.requester.excessivelySafeCall(...);
```

Alternatively, move the existence check into `requestHelper` and revert if `msg.sender` has no code when `callbackGasLimit > 0`, preventing the inconsistent state from being created. [6](#0-5) 

---

### Proof of Concept

1. Deploy `AttackerConsumer` that calls `requestV2(provider, userContribution, 100_000)` and immediately self-destructs in the same transaction (or in a follow-up transaction before the provider reveals).
2. Provider calls `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
3. Execution enters the `gasLimit10k != 0` branch.
4. `req.requester.excessivelySafeCall(...)` targets the now-empty address → returns `(true, [])`.
5. `clearRequest` is called; `RevealedWithCallback` is emitted with `failed = false`.
6. `AttackerConsumer._entropyCallback` was never executed; the random number is permanently lost. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L214-284)
```text
    function requestHelper(
        address provider,
        bytes32 userCommitment,
        bool useBlockhash,
        bool isRequestWithCallback,
        uint32 callbackGasLimit
    ) internal returns (EntropyStructsV2.Request storage req) {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];
        if (_state.providers[provider].sequenceNumber == 0)
            revert EntropyErrors.NoSuchProvider();

        // Assign a sequence number to the request
        uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
        if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.OutOfRandomness();
        providerInfo.sequenceNumber += 1;

        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);

        // Store the user's commitment so that we can fulfill the request later.
        // Warning: this code needs to overwrite *every* field in the request, because the returned request can be
        // filled with arbitrary data.
        req = allocRequest(provider, assignedSequenceNumber);
        req.provider = provider;
        req.sequenceNumber = assignedSequenceNumber;
        req.numHashes = SafeCast.toUint32(
            assignedSequenceNumber -
                providerInfo.currentCommitmentSequenceNumber
        );
        if (
            providerInfo.maxNumHashes != 0 &&
            req.numHashes > providerInfo.maxNumHashes
        ) {
            revert EntropyErrors.LastRevealedTooOld();
        }
        req.commitment = keccak256(
            bytes.concat(userCommitment, providerInfo.currentCommitment)
        );
        req.requester = msg.sender;

        req.blockNumber = SafeCast.toUint64(block.number);
        req.useBlockhash = useBlockhash;

        req.callbackStatus = isRequestWithCallback
            ? EntropyStatusConstants.CALLBACK_NOT_STARTED
            : EntropyStatusConstants.CALLBACK_NOT_NECESSARY;
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
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L358-390)
```text
    function requestV2(
        address provider,
        bytes32 userContribution,
        uint32 gasLimit
    ) public payable override returns (uint64) {
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            constructUserCommitment(userContribution),
            // If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
            // If we remove the blockHash from this, the provider would have no choice but to provide its committed
            // random number. Hence, useBlockHash is set to false.
            false,
            true,
            gasLimit
        );

        emit RequestedWithCallback(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            EntropyStructConverter.toV1Request(req)
        );
        emit EntropyEventsV2.Requested(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            uint32(req.gasLimit10k) * TEN_THOUSAND,
            bytes("")
        );
        return req.sequenceNumber;
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-621)
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L662-681)
```text
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

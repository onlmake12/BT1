### Title
Entropy `revealWithCallback` Legacy Path Has No Exception Handling for Consumer Callback, Permanently Freezing Requests — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.sol`'s `revealWithCallback()` contains two execution paths. The **legacy path** (taken when `req.gasLimit10k == 0`, i.e., the provider has not called `setDefaultGasLimit`) invokes the consumer's `_entropyCallback` directly with no `try/catch`. If the consumer callback reverts, the entire `revealWithCallback` transaction reverts, leaving the request permanently active and unfulfillable. There is no recovery mechanism for this path.

---

### Finding Description

`revealWithCallback` branches on whether `req.gasLimit10k != 0` AND `callbackStatus == CALLBACK_NOT_STARTED`: [1](#0-0) 

When this condition is **false** (i.e., `req.gasLimit10k == 0`), execution falls to the legacy `else` branch: [2](#0-1) 

In this branch, `clearRequest` is called first (CEI pattern), then `_entropyCallback` is called **directly** with no `try/catch`. If the consumer reverts, the entire transaction reverts — including the `clearRequest` — leaving the request permanently stuck.

The `gasLimit10k == 0` condition is the default for all providers that have not explicitly called `setDefaultGasLimit`. The `requestHelper` function documents this: [3](#0-2) 

This means **all requests made through legacy providers** (those with `defaultGasLimit == 0`) are permanently unfulfillable if the consumer's callback reverts.

The new (safe) path uses `excessivelySafeCall` to catch reverts and transitions the request to `CALLBACK_FAILED` state, enabling recovery. The legacy path has no equivalent mechanism. [4](#0-3) 

The test `testRequestWithCallbackAndRevealWithCallbackFailing` (line 999) confirms: when `gasLimit10k == 0` and the callback reverts, `revealWithCallback` simply reverts with no recovery path. [5](#0-4) 

---

### Impact Explanation

Any consumer contract whose `_entropyCallback` reverts (due to a bug, out-of-gas, or any revert condition) will have its request permanently stuck when using a legacy provider (`defaultGasLimit == 0`). The consumer contract never receives its random number. Any funds or state in the consumer contract that depend on receiving the callback (e.g., a lottery holding user deposits, a game awaiting randomness) are permanently frozen. The fee paid by the user is also non-refundable. There is no admin escape hatch or recovery function for this state.

---

### Likelihood Explanation

Medium. The default value of `defaultGasLimit` for a newly registered provider is `0`, meaning all providers that have not explicitly opted into the new gas-limit flow use the legacy path. Any consumer contract with a reverting callback — whether due to a bug, an upgrade that changes behavior, or deliberate griefing by a malicious consumer — triggers the freeze. The entry point (`revealWithCallback`) is permissionless and callable by anyone. [6](#0-5) 

---

### Recommendation

Wrap the legacy callback invocation in a `try/catch` block, mirroring the exception handling already present in the new gas-limit path. On revert, either emit a failure event and leave the request in a recoverable state, or clear the request and emit a failure event. This ensures `revealWithCallback` never reverts due to consumer-side failures, regardless of which execution path is taken.

---

### Proof of Concept

1. Provider registers without calling `setDefaultGasLimit` → `provider.defaultGasLimit == 0`.
2. Consumer contract calls `requestWithCallback(provider, userRandomNumber)` paying the required fee. `requestHelper` sets `req.gasLimit10k = 0`.
3. Provider calls `revealWithCallback(provider, seqNum, userContrib, providerContrib)`.
4. Since `req.gasLimit10k == 0`, the legacy `else` branch executes: `clearRequest` is called, then `IEntropyConsumer(callAddress)._entropyCallback(...)` is called directly.
5. Consumer's `_entropyCallback` reverts (e.g., out-of-gas or logic revert).
6. Entire transaction reverts. `clearRequest` is undone. Request remains active.
7. Any subsequent call to `revealWithCallback` with the same arguments repeats step 4–6 indefinitely.
8. The request is permanently stuck. The consumer never receives its random number. Any funds held by the consumer pending the callback are frozen. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-271)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L542-547)
```text
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-577)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L579-599)
```text
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

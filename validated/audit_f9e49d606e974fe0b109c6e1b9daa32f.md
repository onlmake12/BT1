### Title
Legacy `revealWithCallback` Path Permanently Locks User Funds When Callback Reverts — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, `revealWithCallback` has two execution paths. The new path (when `gasLimit10k != 0`) uses `excessivelySafeCall` and a `CALLBACK_FAILED` recovery state to handle reverting callbacks gracefully. The legacy path (when `gasLimit10k == 0`) calls `_entropyCallback` directly with no error handling. When the callback reverts in the legacy path, the entire `revealWithCallback` transaction reverts, leaving the request permanently alive but undeliverable, with no recovery mechanism and no refund path for the user's fee.

---

### Finding Description

`revealWithCallback` branches on `req.gasLimit10k`:

**New path** (`gasLimit10k != 0`, lines 574–660): uses `excessivelySafeCall` to catch reverts, emits `CallbackFailed`, and sets `callbackStatus = CALLBACK_FAILED`. The request stays alive and can be retried or recovered. [1](#0-0) 

**Legacy path** (`gasLimit10k == 0`, lines 661–702): calls `_entropyCallback` directly with no try/catch. `clearRequest` is called first (CEI pattern), but if the callback reverts, the entire transaction reverts — including `clearRequest` — so the request is not cleared. The request stays alive but there is no `CALLBACK_FAILED` state, no retry state machine, and no refund mechanism. [2](#0-1) 

The legacy path is triggered whenever a provider has not configured a `defaultGasLimit` (the default for all newly registered providers is `0`). Users calling `requestWithCallback` against such providers always land in this path: [3](#0-2) 

The existing test `testRequestWithCallbackAndRevealWithCallbackFailing` explicitly confirms that `revealWithCallback` reverts when the callback reverts in the legacy path, with no state transition: [4](#0-3) 

---

### Impact Explanation

When a user's callback contract reverts (due to a bug, a failed external call, a self-destruct, or any logic error), the provider's keeper cannot successfully call `revealWithCallback`. The request is permanently stuck:

- The user's fee is locked in the Entropy contract with no refund path.
- The random number is never delivered.
- There is no `CALLBACK_FAILED` state for legacy requests, so no recovery flow exists.
- No cancellation or withdrawal function exists for in-flight requests.

This is a direct loss of user funds (the paid entropy fee) with no recourse.

---

### Likelihood Explanation

- Any provider that has not explicitly called `setDefaultGasLimit` has `defaultGasLimit = 0`, which is the default state for all providers.
- Any user whose callback contract reverts for any reason (including a contract upgrade that introduces a bug, or a dependency that starts reverting) will have their request permanently stuck.
- The documentation warns that callbacks must never revert, but this is a developer-facing advisory, not an on-chain enforcement. Real-world callback bugs are common.
- The new path (`gasLimit10k != 0`) already handles this correctly, demonstrating that the protocol designers recognized the problem — but the legacy path was not updated consistently.

---

### Recommendation

Apply the same `excessivelySafeCall` + `CALLBACK_FAILED` recovery mechanism to the legacy path (`gasLimit10k == 0`), or add a request cancellation/refund function that allows users to reclaim their fee after a configurable timeout when a request cannot be fulfilled.

---

### Proof of Concept

1. Provider registers without calling `setDefaultGasLimit` → `defaultGasLimit = 0`.
2. User deploys a callback contract whose `_entropyCallback` always reverts.
3. User calls `requestWithCallback(provider, userRandomNumber)` → request stored with `gasLimit10k = 0`.
4. Provider calls `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`:
   - Enters the `else` (legacy) branch at line 661.
   - `clearRequest` is called at line 666.
   - `_entropyCallback` is called at line 676 → reverts.
   - Entire transaction reverts → `clearRequest` is undone → request stays alive.
5. Provider retries → same revert every time.
6. User's fee is permanently locked. No `CALLBACK_FAILED` state exists. No refund function exists.

This is confirmed by `testRequestWithCallbackAndRevealWithCallbackFailing`: [4](#0-3)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-599)
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

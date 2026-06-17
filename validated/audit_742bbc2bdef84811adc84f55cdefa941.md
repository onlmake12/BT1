### Title
Permanent Fee Lock in `Entropy.sol::revealWithCallback` Legacy Path When Consumer Callback Permanently Reverts — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol::revealWithCallback`, a legacy code path (triggered when `req.gasLimit10k == 0`) calls `IEntropyConsumer._entropyCallback` directly without any error handling. If the consumer contract's callback permanently reverts, the request can never be fulfilled and the user's paid fee is permanently locked in the contract with no refund or cancel mechanism.

---

### Finding Description

`revealWithCallback` contains two distinct execution branches:

**Branch 1 — New safe path** (`req.gasLimit10k != 0` AND `callbackStatus == CALLBACK_NOT_STARTED`):
Uses `excessivelySafeCall` to catch reverts, emits `CallbackFailed`, and transitions the request to `CALLBACK_FAILED` state, allowing future retry. [1](#0-0) 

**Branch 2 — Legacy unsafe path** (`req.gasLimit10k == 0`):
Calls `_entropyCallback` directly with no try/catch: [2](#0-1) 

The `gasLimit10k == 0` condition is set in `requestHelper` when the provider has `defaultGasLimit == 0`: [3](#0-2) 

This means any provider that has not called `setDefaultGasLimit` routes all requests through the legacy path. In that path:

1. `clearRequest` is called **before** the callback (checks-effects-interactions pattern).
2. If `_entropyCallback` reverts, the entire transaction reverts, restoring the request in storage.
3. However, if the consumer's callback **permanently** reverts (e.g., the consumer contract is paused, upgraded with a breaking change, or has a logic bug), every subsequent `revealWithCallback` call will also revert.
4. There is no `cancelRequest`, `withdrawRequest`, or fee-refund function anywhere in the contract.

The user's fee — already credited to the provider and Pyth fee balances at request time — is permanently locked with no recovery path.

The existing test `testRequestWithCallbackAndRevealWithCallbackFailing` confirms this behavior: when `requestWithCallback` is used against a provider with no `defaultGasLimit`, a reverting callback causes `revealWithCallback` to revert indefinitely: [4](#0-3) 

The Pyth documentation itself acknowledges this risk: *"Callback reverts: Your `entropyCallback` must NEVER revert — if it errors, the keeper cannot invoke it."* [5](#0-4) 

---

### Impact Explanation

A user who calls `requestWithCallback` against a legacy provider (one with `defaultGasLimit == 0`) and whose consumer contract's `_entropyCallback` permanently reverts will have their fee permanently locked. The fee is credited to the provider and Pyth at request time and there is no on-chain mechanism to reclaim it. This constitutes **permanent freezing of user funds** (the request fee).

---

### Likelihood Explanation

- Any provider that has not opted into the new gas-limit flow (i.e., has `defaultGasLimit == 0`) triggers this path. Legacy providers deployed before `setDefaultGasLimit` was introduced fall into this category.
- Consumer contracts can permanently revert due to: a logic bug introduced in an upgrade, an intentional pause/emergency stop, or self-destruction.
- The entry path requires no privileged access — any unprivileged user calling `requestWithCallback` can end up in this state.

---

### Recommendation

Wrap the legacy callback invocation in a try/catch block analogous to the new path, or introduce a `cancelRequest` / fee-refund function that allows users to reclaim their fee when a request has been stuck for a configurable timeout period. Alternatively, enforce that all providers must set a non-zero `defaultGasLimit` before accepting new requests, eliminating the legacy path entirely.

---

### Proof of Concept

The existing test already demonstrates the issue. When a provider has no `defaultGasLimit` set (legacy path, `gasLimit10k == 0`) and the consumer callback reverts, `revealWithCallback` reverts on every attempt:

```solidity
// From Entropy.t.sol testRequestWithCallbackAndRevealWithCallbackFailing
EntropyConsumer consumer = new EntropyConsumer(address(random), true); // reverts = true
uint64 assignedSequenceNumber = random.requestWithCallback{value: fee}(
    provider1,
    userRandomNumber
);
// provider1 has no defaultGasLimit set → gasLimit10k == 0 → legacy path
vm.expectRevert();
random.revealWithCallback(
    provider1,
    assignedSequenceNumber,
    userRandomNumber,
    provider1Proofs[assignedSequenceNumber]
); // Always reverts. Fee is permanently locked. No refund path exists.
``` [4](#0-3) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-272)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
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

**File:** apps/developer-hub/src/app/llms-entropy.txt/route.ts (L169-169)
```typescript
2. **Callback reverts**: Your \`entropyCallback\` must NEVER revert — if it errors, the keeper cannot invoke it
```

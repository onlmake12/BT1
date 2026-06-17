### Title
Malicious Entropy Consumer Can Permanently Block `revealWithCallback` via Unbounded External Call in Legacy Path — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary
The legacy execution branch of `revealWithCallback` in `Entropy.sol` (triggered when `req.gasLimit10k == 0`) makes a direct, unbounded external call to the requester contract with no gas cap and no `try/catch`. A malicious requester contract that always reverts or exhausts all forwarded gas causes every `revealWithCallback` invocation for that request to revert, permanently blocking fulfillment and wasting the provider keeper's gas on every retry.

### Finding Description
`revealWithCallback` contains two distinct execution paths:

**New path** (`req.gasLimit10k != 0`): uses `excessivelySafeCall` with an explicit gas cap, catches failures, and transitions the request to `CALLBACK_FAILED` state so the provider can move on.

**Legacy path** (`req.gasLimit10k == 0`): clears the request first, then calls the requester contract directly with no gas cap and no error handling:

```solidity
clearRequest(provider, sequenceNumber);
// ...
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(   // ← unbounded, no try/catch
        sequenceNumber,
        provider,
        randomNumber
    );
}
``` [1](#0-0) 

Because there is no `try/catch`, any revert inside `_entropyCallback` (including an out-of-gas revert) propagates all the way up and reverts the entire transaction — including the preceding `clearRequest`. The request is therefore never cleared and remains permanently active.

The legacy path is reached whenever `req.gasLimit10k == 0`, which occurs when the provider has not configured a `defaultGasLimit` and the user calls `requestWithCallback` (or `requestV2` with `gasLimit == 0`). Provider registration and `setDefaultGasLimit` are both permissionless, so a provider can exist with `defaultGasLimit == 0`. [2](#0-1) 

By contrast, the new path uses `excessivelySafeCall` and the `CALLBACK_FAILED` state machine to handle exactly this scenario: [3](#0-2) 

The legacy path has no equivalent protection.

### Impact Explanation
- The malicious requester's request is permanently stuck: the provider's keeper bot will retry `revealWithCallback` indefinitely, burning gas each time, with no way to clear the request on-chain.
- The user's paid fee is locked in the contract with no refund path.
- A single attacker can submit many such requests (each paying the fee) to amplify keeper gas waste and delay fulfillment of legitimate requests queued behind them.
- The provider cannot retroactively fix existing stuck requests by setting `defaultGasLimit`; that only affects future requests (the `gasLimit10k` field is baked into each request at creation time).

### Likelihood Explanation
- Entry path is fully permissionless: any address can call `requestWithCallback` and supply a malicious contract as the requester.
- The condition `req.gasLimit10k == 0` is reachable whenever a provider has not set a default gas limit — a common state for providers that have not yet migrated to the V2 gas-limit flow.
- The attacker pays the entropy fee, making the attack economically bounded but not prohibitive for a motivated griever.

### Recommendation
**Short term**: Wrap the legacy callback in a `try/catch` (or use `excessivelySafeCall`) so that a reverting callback does not roll back `clearRequest`. Emit a `CallbackFailed` event on failure, consistent with the new path.

**Long term**: Deprecate the legacy path entirely. Require all providers to set a non-zero `defaultGasLimit`, and reject `requestWithCallback` calls that would result in `gasLimit10k == 0`.

### Proof of Concept
1. Deploy `MaliciousConsumer` implementing `IEntropyConsumer` whose `_entropyCallback` runs an infinite loop (consuming all gas).
2. Ensure the target provider has `defaultGasLimit == 0` (or register a fresh provider without calling `setDefaultGasLimit`).
3. Call `requestWithCallback{value: fee}(provider, userRandom)` from `MaliciousConsumer`. The stored request will have `gasLimit10k == 0`.
4. The provider's keeper calls `revealWithCallback(provider, seq, userRandom, providerRevelation)`.
5. Execution reaches the legacy branch, calls `MaliciousConsumer._entropyCallback(...)` with no gas cap, exhausts all gas, and the entire transaction reverts.
6. `clearRequest` is rolled back; the request remains active. Step 4 can be repeated indefinitely — every attempt reverts and wastes the keeper's gas.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-577)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L578-599)
```text
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

### Title
Legacy `revealWithCallback` Path Propagates Callback Reverts, Permanently Blocking Entropy Fulfillment - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

In `Entropy.sol`, the `revealWithCallback` function has a legacy execution path (entered when `req.gasLimit10k == 0`) that calls `_entropyCallback` directly without any revert protection. If the callback reverts for any reason, the entire `revealWithCallback` transaction reverts, permanently blocking fulfillment of that request. This is the direct analog to the `fulfillRandomWords` issue in the external report.

### Finding Description

`revealWithCallback` branches on `req.gasLimit10k`:

- **New path** (`gasLimit10k != 0`): Uses `excessivelySafeCall` to catch reverts, emits `CallbackFailed`, and transitions the request to `CALLBACK_FAILED` state for recovery.
- **Legacy path** (`gasLimit10k == 0`): Calls `IEntropyConsumer(callAddress)._entropyCallback(...)` directly with no try/catch and no revert protection.

```solidity
// Legacy path — lines 661-702
address callAddress = req.requester;
...
clearRequest(provider, sequenceNumber);   // cleared first
// WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(   // bare call, no protection
        sequenceNumber,
        provider,
        randomNumber
    );
}
```

Because `clearRequest` is called before the callback, and Solidity reverts roll back all state changes, a reverting callback causes `clearRequest` to also be rolled back — the request remains active but can never be fulfilled, since every subsequent `revealWithCallback` call will also revert.

The legacy path is entered when a provider has `defaultGasLimit == 0` (i.e., has not called `setDefaultGasLimit`). Any user who calls `requestWithCallback` against such a provider receives a request with `gasLimit10k == 0`, and if their callback reverts (due to a bug, OOG, or intentional design), the request is permanently stuck.

This is confirmed by the existing test `testRequestWithCallbackAndRevealWithCallbackFailing`:

```solidity
// provider has defaultGasLimit == 0 (not set), callback reverts
vm.expectRevert();
random.revealWithCallback(provider1, assignedSequenceNumber, ...);
```

### Impact Explanation

**High.** Any Entropy request made to a legacy provider (one with `defaultGasLimit == 0`) whose callback reverts is permanently unresolvable. The request cannot be cleared, the random number cannot be delivered, and the user's paid fee is effectively lost. There is no recovery path in the legacy flow — unlike the new path which has `CALLBACK_FAILED` state and a retry mechanism.

### Likelihood Explanation

**Low.** Two conditions must hold simultaneously:
1. The chosen provider has not set `defaultGasLimit` (i.e., `defaultGasLimit == 0`), keeping the request in the legacy path.
2. The requester's `entropyCallback` reverts — either due to a bug, an OOG condition caused by underestimating gas, or a malicious self-inflicted design.

Providers that have not upgraded to the new gas-limit flow are the enabling condition. The Pyth default provider is likely upgraded, but permissionless third-party providers may not be.

### Recommendation

1. **Wrap the legacy callback in a try/catch or low-level call** so that a reverting callback does not propagate and block fulfillment:
   ```solidity
   (bool success, ) = callAddress.call(
       abi.encodeWithSelector(IEntropyConsumer._entropyCallback.selector, ...)
   );
   // emit event regardless of success
   ```
2. **Alternatively, deprecate the legacy path entirely** by requiring all providers to set a non-zero `defaultGasLimit` before accepting new `requestWithCallback` calls.
3. **Add a recovery mechanism** for legacy-path requests that are stuck due to a reverting callback (e.g., allow the requester to cancel and reclaim fees after a timeout).

### Proof of Concept

1. Provider registers with `defaultGasLimit == 0` (never calls `setDefaultGasLimit`).
2. User contract calls `requestWithCallback(provider, userContribution)` — this internally calls `requestV2(..., gasLimit=0)`, so `req.gasLimit10k` is set to `0`.
3. User contract's `entropyCallback` reverts (e.g., due to a bug or OOG).
4. Provider/keeper calls `revealWithCallback(provider, sequenceNumber, ...)`.
5. Execution enters the legacy `else` branch at line 661; `clearRequest` executes, then `_entropyCallback` reverts.
6. The entire transaction reverts, rolling back `clearRequest`.
7. The request remains active with `callbackStatus == CALLBACK_NOT_STARTED`, but every future `revealWithCallback` call will also revert.
8. The request is permanently stuck; randomness is never delivered. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-576)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
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

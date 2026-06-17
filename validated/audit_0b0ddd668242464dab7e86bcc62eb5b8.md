### Title
No User Cancellation Mechanism for Unfulfilled Entropy Requests — Fees Permanently Locked on Provider Non-Fulfillment - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

Pyth Entropy has no on-chain mechanism for a user to cancel a pending randomness request and recover their fee. When `requestHelper` is called, the fee is immediately and irrevocably credited to the provider and Pyth protocol. If the provider's keeper service fails to call `revealWithCallback` — or if the legacy V1 path is taken and the consumer callback permanently reverts — the request remains active indefinitely with no user-accessible escape hatch. This is the direct structural analog to the Mode/Chainlink bug: a consumed resource (fee) and a stuck pending state with no cancellation path.

---

### Finding Description

**Fee is immediately credited and non-refundable at request time.**

In `requestHelper` (the internal function called by every `request`, `requestWithCallback`, and `requestV2` variant):

```solidity
// Entropy.sol lines 236-239
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The provider's accrued balance is incremented at the moment of the request, before any fulfillment occurs. The provider can call `withdraw()` and extract this fee immediately, before ever calling `revealWithCallback`. There is no escrow, no hold, and no refund path for the user.

**No cancellation function exists in the contract.**

A search across all `entropy/*.sol` files finds zero occurrences of `cancel`, `timeout`, `expire`, or `refund` as callable user functions. The only way to clear a request from storage is via `reveal` or `revealWithCallback` — both of which require the provider's secret `providerContribution`. The user has no unilateral path to close the request.

**Legacy V1 path (gasLimit10k == 0) creates an additional permanent-stuck scenario.**

When a provider has `defaultGasLimit == 0`, `requestHelper` sets `req.gasLimit10k = 0`:

```solidity
// Entropy.sol lines 268-271
if (providerInfo.defaultGasLimit == 0) {
    req.gasLimit10k = 0;
}
``` [2](#0-1) 

In `revealWithCallback`, this causes the `else` branch to execute, which clears the request **before** invoking the callback:

```solidity
// Entropy.sol lines 662-680
address callAddress = req.requester;
...
clearRequest(provider, sequenceNumber);
// WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED
...
IEntropyConsumer(callAddress)._entropyCallback(...);
``` [3](#0-2) 

If the consumer callback reverts, the entire transaction reverts (no `try/catch`), `clearRequest` is also reverted, and the request remains active. Unlike the V2 path (which uses `excessivelySafeCall` and transitions to `CALLBACK_FAILED` for recovery), the V1 path has **no recovery state**. The request is permanently stuck: the provider cannot fulfill it (every attempt reverts), and the user has no cancellation path.

The V2 path does have a `CALLBACK_FAILED` recovery state:
```solidity
// Entropy.sol lines 553-558
if (
    !(req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED ||
      req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
) {
    revert EntropyErrors.InvalidRevealCall();
}
``` [4](#0-3) 

But this recovery only applies when `req.gasLimit10k != 0`. Legacy V1 requests and any request to a provider with `defaultGasLimit == 0` are excluded.

---

### Impact Explanation

A user who calls `requestWithCallback` or `requestV2` against a provider with `defaultGasLimit == 0`:

1. Pays a non-refundable fee (immediately credited to provider and Pyth).
2. Receives a sequence number representing a pending request.
3. If the provider's keeper service goes offline, has a bug, or if the consumer callback permanently reverts, the request is stuck indefinitely.
4. The user has no on-chain mechanism to cancel the request, recover the fee, or resubmit with the same parameters.
5. The provider can withdraw the fee at any time, leaving the user with no recourse.

**Impact**: Permanent loss of user-paid fees; denial of the random number service the user paid for. For integrating protocols (e.g., NFT minting, gaming), this translates to users losing game state or rewards tied to the pending sequence number — directly mirroring the Mode mystery-box scenario.

---

### Likelihood Explanation

- Any provider registered with `defaultGasLimit == 0` (the legacy default) triggers the V1 path for all `requestWithCallback` calls.
- A consumer contract whose `_entropyCallback` reverts due to a logic bug (out-of-gas, failed require, storage collision) permanently blocks fulfillment on the V1 path.
- Provider keeper outages are realistic operational events (network congestion, infrastructure failure, key rotation errors).
- The fee non-refundability is unconditional and affects every single request regardless of path.
- An unprivileged user triggers this by simply calling `requestWithCallback{value: fee}(provider, userContribution)` — no special access required.

---

### Recommendation

1. **Add a user-callable `cancelRequest` function** with a minimum block-age threshold (e.g., 256 blocks after request), analogous to Chainlink's `cancelChainlinkRequest`. This function should refund the user's fee from the contract's balance and clear the request.
2. **Escrow fees** rather than immediately crediting the provider, releasing them only upon successful fulfillment or after a timeout.
3. **Migrate all paths to the V2 gasLimit flow**: enforce `req.gasLimit10k > 0` for all new requests so the `CALLBACK_FAILED` recovery state is always available.
4. **Deprecate and gate the V1 path**: reject `requestWithCallback` calls to providers with `defaultGasLimit == 0`, or automatically assign a minimum gas limit.

---

### Proof of Concept

```
1. Deploy a provider with defaultGasLimit = 0 (legacy registration).
2. Deploy a consumer contract whose _entropyCallback always reverts.
3. Consumer calls requestWithCallback{value: fee}(provider, userContribution).
   → requestHelper credits fee to provider immediately (line 237).
   → req.gasLimit10k = 0 (line 270).
4. Provider calls revealWithCallback(provider, seqNum, userContribution, providerContribution).
   → else branch taken (line 661).
   → clearRequest() called (line 666).
   → _entropyCallback() reverts (line 676).
   → Entire transaction reverts; clearRequest is undone.
   → Request remains active with CALLBACK_NOT_NECESSARY status.
5. Provider retries → same revert every time.
6. User has no cancelRequest() to call.
7. Provider calls withdraw(providerFee) → fee extracted, user has zero recourse.
8. Request is permanently stuck; user's ETH is permanently lost.
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L236-239)
```text
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-271)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L553-558)
```text
        if (
            !(req.callbackStatus ==
                EntropyStatusConstants.CALLBACK_NOT_STARTED ||
                req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
        ) {
            revert EntropyErrors.InvalidRevealCall();
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

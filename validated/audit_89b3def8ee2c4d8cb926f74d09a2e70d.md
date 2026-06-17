### Title
Entropy Fee Permanently Lost When Callback Permanently Fails — No Cancellation or Refund Mechanism (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In Pyth's Entropy protocol, the user's fee is fully accrued to the provider and Pyth at **request time** (step 1). If the user's callback contract permanently reverts during the reveal (step 2), the request becomes stuck with no cancellation or refund path, causing permanent loss of the paid fee with no service rendered.

---

### Finding Description

`requestHelper` immediately and irrevocably splits the entire `msg.value` between the provider and Pyth at the moment of the request:

```solidity
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

There is no refund of any excess value, as explicitly documented across the interface:

> "excess value is *not* refunded to the caller" [2](#0-1) 

**Legacy path (`gasLimit10k == 0`):** In `revealWithCallback`, the legacy branch calls `_entropyCallback` directly with no error catching:

```solidity
clearRequest(provider, sequenceNumber);
// WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber, provider, randomNumber
    );
}
``` [3](#0-2) 

If the callback always reverts, the entire `revealWithCallback` transaction reverts (including the `clearRequest`), so the request is never cleared. The provider cannot successfully fulfill it. The fee is already accrued and cannot be recovered.

**New V2 path (`gasLimit10k != 0`):** The callback is wrapped in `excessivelySafeCall`, and a permanently-failing callback transitions the request to `CALLBACK_FAILED` state:

```solidity
req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED;
``` [4](#0-3) 

`revealWithCallback` can be retried from `CALLBACK_FAILED` state, but if the callback is permanently broken (e.g., the requester contract is self-destructed, bricked, or has an unfixable logic error), the request stays in `CALLBACK_FAILED` forever. The fee is permanently lost with no service rendered and no refund path. [5](#0-4) 

There is no `cancelRequest`, `refundRequest`, or any other mechanism in the contract that allows a user to recover their fee from a stuck request.

---

### Impact Explanation

A user who has paid the Entropy fee loses that fee permanently if their callback contract is permanently non-functional. They receive no random number and no refund. The fee amount is bounded by the provider's configured fee, so the loss per request is limited (analogous to the WeVE report's "loss limited to the capped amount"), but it is a real, irreversible loss of user funds with no recourse.

---

### Likelihood Explanation

Realistic triggering scenarios include:

- A user's callback contract has a logic bug that causes it to always revert (e.g., an assertion on the random number value, a broken state machine, or a missing `receive()` function if ETH is forwarded).
- A user's callback contract is upgraded to a broken implementation between request and reveal.
- A user's callback contract is self-destructed after the request is made.
- A user accidentally sends significantly more ETH than `requiredFee` (the entire excess is permanently captured by Pyth with no refund).

The Fortuna keeper documentation itself acknowledges this: "Your `entropyCallback` must NEVER revert — if it errors, the keeper cannot invoke it." [6](#0-5) 

This warning confirms the scenario is realistic and known.

---

### Recommendation

1. **Add a `cancelRequest` function** that allows the original requester to cancel a stuck request (in `CALLBACK_NOT_STARTED` or `CALLBACK_FAILED` state after a timeout) and receive a refund of the fee minus a small penalty.
2. **Refund excess `msg.value`** beyond `requiredFee` in `requestHelper` rather than silently capturing it as Pyth fees.
3. For the legacy path, consider migrating all providers to the V2 path (non-zero `defaultGasLimit`) to at least allow retries.

---

### Proof of Concept

1. User calls `requestV2{value: fee}(provider, userRandomNumber, gasLimit)`. Fee is immediately accrued: `providerInfo.accruedFeesInWei += providerFee` and `_state.accruedPythFeesInWei += (msg.value - providerFee)`.
2. User's callback contract has a bug: `entropyCallback` always reverts.
3. Fortuna keeper calls `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
4. The `excessivelySafeCall` catches the revert; `CallbackFailed` event is emitted; `req.callbackStatus = CALLBACK_FAILED`.
5. Keeper retries — same result. Request stays in `CALLBACK_FAILED` forever.
6. User's fee is permanently held by the contract. User has no `cancelRequest` function to call. Fee is lost. [7](#0-6) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L233-239)
```text
        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L621-651)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L663-681)
```text
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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L18-19)
```text
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** apps/developer-hub/src/app/llms-entropy.txt/route.ts (L169-169)
```typescript
2. **Callback reverts**: Your \`entropyCallback\` must NEVER revert — if it errors, the keeper cannot invoke it
```

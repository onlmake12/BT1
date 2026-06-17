### Title
Malicious Caller Can Permanently Brick Echo Callback by Supplying Insufficient Gas — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.executeCallback` is permissionlessly callable after the exclusivity period. It clears the request and credits the provider **before** invoking the user's callback. There is no `gasleft()` guard to ensure the outer context holds enough gas to forward `req.callbackGasLimit` to the inner call. A malicious actor can supply precisely calibrated gas: enough for all pre-callback logic to complete (clearing the request, crediting the provider) but not enough to satisfy the 63/64-rule forwarding requirement for the callback. The `try/catch` silently swallows the out-of-gas revert, the request is permanently deleted, and the user's callback never executes with no retry path.

---

### Finding Description

`Echo.executeCallback` performs the following sequence:

1. Validates provider exclusivity and price IDs
2. Calls `pyth.parsePriceFeedUpdates` (external call, consumes gas)
3. Credits provider fees: `_state.providers[providerToCredit].accruedFeesInWei += ...`
4. **Clears the request**: `clearRequest(sequenceNumber)` — request is now permanently gone
5. Advances `_state.firstUnfulfilledSeq` (storage loop)
6. Attempts the callback: `IEchoConsumer(req.requester)._echoCallback{gas: req.callbackGasLimit}(...)`
7. Catches any failure silently and emits `PriceUpdateCallbackFailed` [1](#0-0) 

There is **no `gasleft()` check** before step 6 to verify that the remaining gas is sufficient to forward `req.callbackGasLimit` to the inner call. The EVM's 63/64 rule means that if `gasleft()` at the point of the `try` is less than `callbackGasLimit * 64/63`, the inner call receives fewer gas units than `callbackGasLimit`. The callback then runs out of gas, the `catch` block fires, and `PriceUpdateCallbackFailed` is emitted — but the request was already cleared in step 4 and cannot be retried. [2](#0-1) 

The interface comment acknowledges this risk ("Requires 1.5x the callback gas limit to account for cross-contract call overhead") but this is documentation only — it is **not enforced on-chain**. [3](#0-2) 

Compare this to `Entropy.revealWithCallback`, which correctly handles this with:
- A post-call gas sufficiency check: `(startingGas * 31) / 32 > uint256(req.gasLimit10k) * TEN_THOUSAND`
- A `revert EntropyErrors.InsufficientGas()` path that prevents the request from being cleared
- A `CALLBACK_FAILED` retry state [4](#0-3) 

Echo has none of these protections.

---

### Impact Explanation

A user submits a request via `requestPriceUpdatesWithCallback` with a `callbackGasLimit` sized for their application logic (e.g., 500,000 gas). A malicious actor calls `executeCallback` with gas calibrated to:

- Complete all pre-callback logic (storage reads, `parsePriceFeedUpdates`, fee crediting, `clearRequest`, `firstUnfulfilledSeq` loop) — perhaps ~200,000–400,000 gas
- Leave fewer than `callbackGasLimit * 64/63` gas remaining at the point of the `try` call

The inner callback receives less than `callbackGasLimit` gas, reverts out-of-gas, the `catch` block fires, and the request is permanently deleted. The user's application logic (minting, state updates, game logic, etc.) never executes. The user has paid the full fee and has no recourse — there is no retry mechanism in Echo.

---

### Likelihood Explanation

`executeCallback` is permissionlessly callable by anyone after the exclusivity period (default 15 seconds). [5](#0-4) 

Any unprivileged actor can observe a pending request on-chain, simulate the gas cost of the pre-callback logic, and submit `executeCallback` with a precisely calibrated `{gas: N}` value. This requires no special access, no leaked keys, and no collusion. The attack is cheap (the attacker pays only the gas for the outer function) and permanently destroys the victim's callback.

---

### Recommendation

Add a `gasleft()` guard before the callback invocation, analogous to Entropy's approach. Before the `try` block, require:

```solidity
require(
    gasleft() >= uint256(req.callbackGasLimit) * 64 / 63 + OVERHEAD_BUFFER,
    "Echo: insufficient gas to execute callback"
);
```

Where `OVERHEAD_BUFFER` accounts for gas consumed between the check and the actual `CALL` opcode. Alternatively, if the check fails, revert the entire transaction (do not clear the request) so the request remains retryable. This is the pattern used by `Entropy.revealWithCallback` via `revert EntropyErrors.InsufficientGas()`. [6](#0-5) 

---

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback` with `callbackGasLimit = 500_000`. Request is stored with sequence number `N`.
2. Exclusivity period (15 s) elapses.
3. Attacker simulates `executeCallback` to measure gas consumed by steps 1–5 above (say ~300,000 gas).
4. Attacker calls `executeCallback{gas: 350_000}(...)` — enough for pre-callback logic, but `gasleft()` at the `try` is ~50,000, far below `500_000 * 64/63 ≈ 507,937`.
5. Inner call receives ~50,000 * 63/64 ≈ 49,219 gas — far less than 500,000. Callback reverts out-of-gas.
6. `catch` block fires, emits `PriceUpdateCallbackFailed("low-level error (possibly out of gas)")`.
7. Request was cleared at step 4 above — permanently gone. User's callback never executes. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L113-121)
```text
        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-201)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);

        // TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number.
        // a better solution would be a doubly-linked list of active requests.
        // After successful callback, update firstUnfulfilledSeq if needed
        while (
            _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
            !isActive(findRequest(_state.firstUnfulfilledSeq))
        ) {
            _state.firstUnfulfilledSeq++;
        }

        try
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
        {
            // Callback succeeded
            emitPriceUpdate(sequenceNumber, priceIds, priceFeeds);
        } catch Error(string memory reason) {
            // Explicit revert/require
            emit PriceUpdateCallbackFailed(
                sequenceNumber,
                providerToCredit,
                priceIds,
                req.requester,
                reason
            );
        } catch {
            // Out of gas or other low-level errors
            emit PriceUpdateCallbackFailed(
                sequenceNumber,
                providerToCredit,
                priceIds,
                req.requester,
                "low-level error (possibly out of gas)"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L62-65)
```text
     * @notice Executes the callback for a price update request
     * @dev Requires 1.5x the callback gas limit to account for cross-contract call overhead
     * For example, if callbackGasLimit is 1M, the transaction needs at least 1.5M gas + some gas for some other operations in the function before the callback
     * @param providerToCredit The provider to credit for fulfilling the request. This may not be the provider that submitted the request (if the exclusivity period has elapsed).
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L621-659)
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
            } else {
                // Callback reverted by (potentially) running out of gas, but the calling context did not have enough gas
                // to run the callback. This is a corner case that can happen due to the nuances of gas passing
                // in calls (see the comment on the call above).
                //
                // (Note that reverting here plays nicely with the estimateGas RPC method, which binary searches for
                // the smallest gas value that causes the transaction to *succeed*. See https://github.com/ethereum/go-ethereum/pull/3587 )
                revert EntropyErrors.InsufficientGas();
```

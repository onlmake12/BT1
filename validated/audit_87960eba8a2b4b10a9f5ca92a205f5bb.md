### Title
Request Cleared Before Callback Execution Allows Attacker to Permanently Brick User Price Update Requests via Insufficient Gas — (File: target_chains/ethereum/contracts/contracts/echo/Echo.sol)

---

### Summary

In `Echo.sol`'s `executeCallback`, the request is cleared and provider fees are credited **before** the consumer callback is attempted. The callback is wrapped in a `try/catch`, so any failure (including out-of-gas) is silently swallowed. Because `executeCallback` is permissionlessly callable by anyone after the exclusivity period, an attacker can call it with insufficient gas, causing the callback to fail silently while the request is permanently deleted with no replay mechanism.

---

### Finding Description

`executeCallback` in `Echo.sol` follows this sequence:

1. Credits fees to the provider: `_state.providers[providerToCredit].accruedFeesInWei += ...`
2. Permanently deletes the request: `clearRequest(sequenceNumber)`
3. Attempts the callback inside a `try/catch`:

```solidity
try
    IEchoConsumer(req.requester)._echoCallback{
        gas: req.callbackGasLimit
    }(sequenceNumber, priceFeeds)
{
    emitPriceUpdate(...);
} catch Error(string memory reason) {
    emit PriceUpdateCallbackFailed(...);
} catch {
    emit PriceUpdateCallbackFailed(...);
}
``` [1](#0-0) 

The developers themselves acknowledge this ordering risk with a TODO comment immediately above the `clearRequest` call:

> "TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert. If executeCallback can revert, then funds can be permanently locked in the contract." [2](#0-1) 

Unlike Entropy's new callback path, which explicitly checks for sufficient gas and reverts with `InsufficientGas` if the calling context cannot forward the required gas limit, Echo has **no such guard**: [3](#0-2) 

Echo also has **no `CALLBACK_FAILED` retry state** — once `clearRequest` executes, the request is gone permanently regardless of callback outcome. [4](#0-3) 

The `executeCallback` function is permissionlessly callable by anyone after the exclusivity period elapses, as documented in the interface:

> "providerToCredit — The provider to credit for fulfilling the request. **This may not be the provider that submitted the request (if the exclusivity period has elapsed).**" [5](#0-4) 

---

### Impact Explanation

An attacker who calls `executeCallback` with a transaction gas limit just sufficient to execute the function body up to and including `clearRequest`, but insufficient to forward `req.callbackGasLimit` to the consumer callback, will:

- Permanently delete the user's request (no replay possible)
- Credit the provider's `accruedFeesInWei` (provider is paid)
- Cause the consumer callback to fail with out-of-gas (silently caught)
- Emit `PriceUpdateCallbackFailed` — the user's fee is gone and the callback never executed

The user paid a fee for a price-update callback service they will never receive, with no recourse.

---

### Likelihood Explanation

- `executeCallback` is a public, permissionless function callable by any address after the exclusivity period.
- An attacker can monitor on-chain events (`RequestedWithCallback`) to identify pending requests.
- The attacker front-runs the legitimate provider's `executeCallback` call with a crafted low-gas transaction.
- The gas cost of the function body up to `clearRequest` is deterministic and estimable off-chain, making the attack straightforward to calibrate.

---

### Recommendation

Add a minimum gas check before the callback, mirroring Entropy's `InsufficientGas` guard:

```solidity
// Ensure the calling context can forward at least req.callbackGasLimit to the callback.
// The EVM forwards at most 63/64 of remaining gas; use 31/32 as a safety margin.
if ((gasleft() * 31) / 32 < uint256(req.callbackGasLimit)) {
    revert InsufficientGas();
}

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-201)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L323-332)
```text
    function clearRequest(uint64 sequenceNumber) internal {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        Request storage req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            req.sequenceNumber = 0;
        } else {
            delete _state.requestsOverflow[key];
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L652-659)
```text
            } else {
                // Callback reverted by (potentially) running out of gas, but the calling context did not have enough gas
                // to run the callback. This is a corner case that can happen due to the nuances of gas passing
                // in calls (see the comment on the call above).
                //
                // (Note that reverting here plays nicely with the estimateGas RPC method, which binary searches for
                // the smallest gas value that causes the transaction to *succeed*. See https://github.com/ethereum/go-ethereum/pull/3587 )
                revert EntropyErrors.InsufficientGas();
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L61-76)
```text
    /**
     * @notice Executes the callback for a price update request
     * @dev Requires 1.5x the callback gas limit to account for cross-contract call overhead
     * For example, if callbackGasLimit is 1M, the transaction needs at least 1.5M gas + some gas for some other operations in the function before the callback
     * @param providerToCredit The provider to credit for fulfilling the request. This may not be the provider that submitted the request (if the exclusivity period has elapsed).
     * @param sequenceNumber The sequence number of the request
     * @param updateData The raw price update data from Pyth
     * @param priceIds The price feed IDs to update, must match the request
     */
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable;

```

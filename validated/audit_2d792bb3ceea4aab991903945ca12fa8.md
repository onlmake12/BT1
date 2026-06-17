### Title
User Fees Permanently Stuck in `Echo` Contract When Provider Fails to Execute Callback — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary
In `Echo.sol`, when a user calls `requestPriceUpdatesWithCallback` and pays ETH, the provider-fee portion (`req.fee = msg.value - pythFeeInWei`) is held in the contract balance. If the assigned provider never calls `executeCallback`, this ETH is permanently locked — there is no cancellation, timeout-based refund, or admin rescue function for unfulfilled request fees.

---

### Finding Description

In `requestPriceUpdatesWithCallback`, the user's ETH is split:

- `_state.accruedFeesInWei += _state.pythFeeInWei` — protocol fee is immediately credited and withdrawable by admin.
- `req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei)` — the remainder is stored in the request struct. [1](#0-0) 

The `req.fee` is only ever credited to a provider inside `executeCallback`:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [2](#0-1) 

If `executeCallback` is never called (provider goes offline, is malicious, or the request simply expires), `req.fee` remains in the contract's ETH balance but is tracked in **no** withdrawable accounting slot — not in `_state.accruedFeesInWei` (protocol fees) and not in any provider's `accruedFeesInWei`. The contract itself acknowledges this gap with a TODO comment:

> "TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback." [3](#0-2) 

The admin's `withdrawFees` only covers `_state.accruedFeesInWei` (protocol fees): [4](#0-3) 

There is no `cancelRequest`, no timeout-based refund, and no rescue path for the `req.fee` of unfulfilled requests.

---

### Impact Explanation

Any ETH paid as the provider-fee component of a `requestPriceUpdatesWithCallback` call that is never fulfilled is permanently locked in the `Echo` contract. Neither the user, the admin, nor any other party can recover it. This is a direct loss of user funds with no recovery path.

**Impact: High** — user ETH is permanently unrecoverable.

---

### Likelihood Explanation

A provider can go offline, be deregistered, or deliberately refuse to fulfill requests. The exclusivity period means only the assigned provider can fulfill during the first N seconds; after that, any provider can fulfill — but if no provider does (e.g., the request is stale or unprofitable), the funds remain stuck. This is a realistic operational scenario.

**Likelihood: Low** — requires provider non-fulfillment, but the protocol has no safeguard against it.

---

### Recommendation

1. Add a `cancelRequest` function that allows the original requester to reclaim `req.fee` after a configurable timeout (e.g., after the exclusivity period plus a grace window).
2. Alternatively, track unfulfilled request fees in a separate accounting slot so the admin can rescue them via governance.
3. Remove the `payable` modifier from `executeCallback` or ensure any `msg.value` sent to it is also properly accounted for or refunded.

---

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback{value: totalFee}(provider, ...)`.
2. `req.fee = totalFee - pythFeeInWei` is stored; `accruedFeesInWei += pythFeeInWei`.
3. Provider goes offline and never calls `executeCallback`.
4. `req.fee` sits in the contract's ETH balance, untracked by any withdrawable accounting.
5. Admin calls `withdrawFees(accruedFeesInWei)` — only recovers the `pythFeeInWei` portion.
6. `req.fee` (the provider-fee portion) is permanently stuck with no recovery path. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-102)
```text
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable override returns (uint64 requestSequenceNumber) {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );

        // FIXME: this comment is wrong. (we're not using tx.gasprice)
        // NOTE: The 60-second future limit on publishTime prevents a DoS vector where
        //      attackers could submit many low-fee requests for far-future updates when gas prices
        //      are low, forcing executors to fulfill them later when gas prices might be much higher.
        //      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
        //      the fee estimation unreliable.
        require(publishTime <= block.timestamp + 60, "Too far in future");
        if (priceIds.length > MAX_PRICE_IDS) {
            revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
        }
        requestSequenceNumber = _state.currentSequenceNumber++;

        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);

        // Create array with the right size
        req.priceIdPrefixes = new bytes8[](priceIds.length);

        // Copy only the first 8 bytes of each price ID to storage
        for (uint8 i = 0; i < priceIds.length; i++) {
            // Extract first 8 bytes of the price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }
            req.priceIdPrefixes[i] = prefix;
        }
        _state.accruedFeesInWei += _state.pythFeeInWei;

        emit PriceUpdateRequested(req, priceIds);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-160)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L289-299)
```text
    function withdrawFees(uint128 amount) external override {
        require(msg.sender == _state.admin, "Only admin can withdraw fees");
        require(_state.accruedFeesInWei >= amount, "Insufficient balance");

        _state.accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }
```

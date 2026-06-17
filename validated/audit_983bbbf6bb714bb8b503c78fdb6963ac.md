### Title
User Fees Permanently Locked When Provider Fails to Execute Callback — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.sol`, user fees paid during `requestPriceUpdatesWithCallback` are stored in the request struct and only credited to the provider upon `executeCallback`. There is no refund path for users and no timeout mechanism. If `executeCallback` is never called — including when the Pyth oracle fee rises post-request making fulfillment unprofitable — user funds are permanently locked in the contract with no recovery.

---

### Finding Description

**Step 1 — Fee stored, not forwarded:**

In `requestPriceUpdatesWithCallback`, the user pays `msg.value`. The Pyth protocol fee is immediately credited, but the provider's portion is stored in the request struct:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
_state.accruedFeesInWei += _state.pythFeeInWei;
``` [1](#0-0) 

The provider's fee (`req.fee`) is **not** credited to `_state.providers[provider].accruedFeesInWei` at request time. It remains locked inside the request struct.

**Step 2 — Fee only released on `executeCallback`:**

The provider's fee is only credited when `executeCallback` is called:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
clearRequest(sequenceNumber);
``` [2](#0-1) 

Here `pythFee = pyth.getUpdateFee(updateData)` is the **live** Pyth oracle fee at execution time, not the fee paid at request time. If the Pyth oracle fee has risen since the request was made, the expression `(req.fee + msg.value) - pythFee` underflows and reverts, making the request permanently unfulfillable.

**Step 3 — No refund mechanism exists:**

The contract provides no function for users to reclaim their locked fees. The only withdrawal functions are:
- `withdrawFees` — admin-only, for Pyth protocol fees
- `withdrawAsFeeManager` — fee manager-only, for provider accrued fees [3](#0-2) [4](#0-3) 

Neither function can return funds to the original requester. The contract's own TODO comment acknowledges the risk:

> `// TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert. If executeCallback can revert, then funds can be permanently locked in the contract.` [5](#0-4) 

---

### Impact Explanation

User funds paid as provider fees are permanently locked in the contract whenever `executeCallback` is not called. There is no timeout, no refund function, and no governance path to recover individual user deposits. The impact is **loss of user funds** — identical in class to the external report where players cannot withdraw earnings because funds were never forwarded to the PayoutManager.

**Impact: High**

---

### Likelihood Explanation

Multiple realistic triggers exist:

1. **Pyth oracle fee increase**: `pythFeeInWei` in Echo is a fixed parameter set at initialization. The actual `pyth.getUpdateFee(updateData)` is dynamic. If the Pyth oracle fee rises after a request is made, `(req.fee + msg.value) - pythFee` underflows, causing `executeCallback` to revert for any caller. The request becomes permanently unfulfillable.

2. **Provider operational failure**: The assigned provider may go offline. After the exclusivity period, any third party may call `executeCallback`, but they must pay the live Pyth fee out of pocket with no guarantee of profit, so they have no economic incentive to do so.

3. **Callback gas exhaustion**: The `try/catch` in `executeCallback` catches consumer callback failures and emits an event, but the request is already cleared and fees already credited to the provider. However, if `executeCallback` itself reverts before `clearRequest` (e.g., due to the underflow above), the request remains active but unfulfillable.

**Likelihood: High**

---

### Recommendation

1. **Add a user refund function** with a configurable timeout (e.g., `block.timestamp > req.publishTime + timeout`), allowing users to reclaim `req.fee` if `executeCallback` has not been called.
2. **Snapshot the Pyth fee at request time** and store it in the request struct, so the fulfillment cost is deterministic and cannot become unprofitable after the fact.
3. **Credit provider fees at request time** (analogous to how Entropy credits `providerInfo.accruedFeesInWei` in `requestHelper`) and deduct from them at callback time to cover the live Pyth oracle fee, rather than leaving user funds in limbo inside the request struct.

---

### Proof of Concept

1. Pyth oracle fee is currently 100 wei. Echo's `pythFeeInWei` is also 100 wei.
2. User calls `requestPriceUpdatesWithCallback{value: 1100}(provider, ...)` — paying 100 wei Pyth fee + 1000 wei provider fee. `req.fee = 1000`.
3. Pyth governance raises the oracle fee to 2000 wei.
4. Provider attempts `executeCallback{value: 0}(...)`: `pythFee = 2000`, `(1000 + 0) - 2000` underflows → revert.
5. Provider attempts `executeCallback{value: 1000}(...)`: `pythFee = 2000`, `(1000 + 1000) - 2000 = 0` — provider earns nothing and must spend 1000 wei. No economic incentive.
6. No third party has incentive to fulfill. User's 1000 wei is permanently locked.
7. User calls `withdrawFees` → reverts ("Only admin can withdraw fees").
8. User has no other recourse. Funds are lost. [6](#0-5) [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-165)
```text
    // TODO: does this need to be payable? Any cost paid to Pyth could be taken out of the provider's accrued fees.
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);

        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }

        // Verify priceIds match
        require(
            priceIds.length == req.priceIdPrefixes.length,
            "Price IDs length mismatch"
        );
        for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
            // Extract first 8 bytes of the provided price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }

            // Compare with stored prefix
            if (prefix != req.priceIdPrefixes[i]) {
                // Now we can directly use the bytes8 prefix in the error
                revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
            }
        }

        // TODO: should this use parsePriceFeedUpdatesUnique? also, do we need to add 1 to maxPublishTime?
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);
        PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
            value: pythFee
        }(
            updateData,
            priceIds,
            SafeCast.toUint64(req.publishTime),
            SafeCast.toUint64(req.publishTime)
        );

        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L360-379)
```text
    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        require(
            msg.sender == _state.providers[provider].feeManager,
            "Only fee manager"
        );
        require(
            _state.providers[provider].accruedFeesInWei >= amount,
            "Insufficient balance"
        );

        _state.providers[provider].accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }
```

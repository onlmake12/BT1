### Title
No Cancellation or Refund Mechanism in Echo Contract Allows Permanent User Fund Lock - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo.sol` contract has no mechanism for users to cancel pending price update requests or recover their funds. If a provider fails to call `executeCallback` — due to unavailable price data at the exact requested timestamp, provider going offline, or malicious inaction — user funds are permanently locked in the contract with no recovery path. The developers themselves acknowledge this risk in a TODO comment in the code.

---

### Finding Description

When a user calls `requestPriceUpdatesWithCallback`, they pay a fee that is stored in `req.fee` inside the contract. The only way to release these funds is through `executeCallback`. However, the contract has no cancellation, timeout, or refund mechanism of any kind.

The critical ordering issue in `executeCallback` is:

1. Provider fee is credited: `_state.providers[providerToCredit].accruedFeesInWei += ...`
2. Request is cleared: `clearRequest(sequenceNumber)`
3. Callback is attempted with try-catch [1](#0-0) 

The developers explicitly acknowledge this in a TODO comment:

> `// TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert. If executeCallback can revert, then funds can be permanently locked in the contract.` [2](#0-1) 

A second critical issue compounds this: `executeCallback` calls `pyth.parsePriceFeedUpdates` with **both** `minPublishTime` and `maxPublishTime` set to exactly `req.publishTime`: [3](#0-2) 

This means the provider must supply price data published at **exactly** the requested timestamp. Pyth does not publish prices at every second; if no price was published at the exact `publishTime`, `parsePriceFeedUpdates` will revert, making the request permanently unfulfillable.

There is no `cancelRequest`, `refundRequest`, or timeout-based recovery function anywhere in the contract: [4](#0-3) 

The only withdrawal functions are `withdrawFees` (admin-only, for Pyth protocol fees) and `withdrawAsFeeManager` (for provider fees) — neither returns funds to the user: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

User funds paid as fees in `requestPriceUpdatesWithCallback` are permanently locked in the Echo contract if:

- The provider cannot find price data published at exactly `req.publishTime` (realistic, since Pyth publishes at intervals, not every second)
- The provider goes offline or is decommissioned
- The provider is malicious and intentionally withholds fulfillment

The `req.fee` field stores the user's payment: [7](#0-6) 

Once locked, there is no on-chain path to recover these funds. This is a direct analog to the original report: just as burning the NFT made `ownerOf()` always revert and permanently locked funds, here the absence of a cancellation path means any unfulfillable request permanently locks user funds.

---

### Likelihood Explanation

**Medium-High.** The exact-timestamp requirement (`minPublishTime == maxPublishTime == req.publishTime`) is a realistic and common failure mode. Pyth price feeds are published at intervals (not every second), so a user requesting `publishTime = block.timestamp` will frequently find no price published at that exact second. Additionally, providers can go offline, and there is no penalty mechanism to incentivize fulfillment (the TODO comment at line 157 acknowledges this gap). Any unprivileged user calling `requestPriceUpdatesWithCallback` is exposed to this risk. [8](#0-7) 

---

### Recommendation

1. **Add a cancellation/refund function** that allows the original requester to cancel a pending request and recover `req.fee` after a configurable timeout period (e.g., after `publishTime + exclusivityPeriod + buffer` has passed with no fulfillment).
2. **Relax the exact-timestamp requirement** in `executeCallback` by using a small time window (e.g., `[publishTime - delta, publishTime + delta]`) instead of requiring an exact match.
3. **Add a penalty mechanism** for providers who fail to fulfill within the exclusivity period, as the TODO comment at line 157 suggests.

---

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback{value: fee}(provider, block.timestamp, priceIds, gasLimit)`. Funds are stored in `req.fee`.
2. Provider attempts `executeCallback(...)` but `pyth.parsePriceFeedUpdates{value: pythFee}(updateData, priceIds, publishTime, publishTime)` reverts because no Pyth price was published at exactly `publishTime`.
3. `executeCallback` reverts entirely; the request remains active.
4. No other path exists to recover the user's funds — no cancel, no timeout refund, no admin rescue for user funds.
5. User's funds are permanently locked in the Echo contract. [9](#0-8)

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

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L37-142)
```text
interface IEcho is EchoEvents {
    // Core functions
    /**
     * @notice Requests price updates with a callback
     * @dev The msg.value must be equal to getFee(callbackGasLimit)
     * @param provider The provider to fulfill the request
     * @param publishTime The minimum publish time for price updates, it should be less than or equal to block.timestamp + 60
     * @param priceIds The price feed IDs to update. Maximum 10 price feeds per request.
     *        Requests requiring more feeds should be split into multiple calls.
     * @param callbackGasLimit The amount of gas allocated for the callback execution
     * @return sequenceNumber The sequence number assigned to this request
     * @dev Security note: The 60-second future limit on publishTime prevents a DoS vector where
     *      attackers could submit many low-fee requests for far-future updates when gas prices
     *      are low, forcing executors to fulfill them later when gas prices might be much higher.
     *      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
     *      the fee estimation unreliable.
     */
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable returns (uint64 sequenceNumber);

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

    // Getters
    /**
     * @notice Gets the base fee charged by Pyth protocol
     * @dev This is a fixed fee per request that goes to the Pyth protocol, separate from gas costs
     * @return pythFeeInWei The base fee in wei that every request must pay
     */
    function getPythFeeInWei() external view returns (uint96 pythFeeInWei);

    /**
     * @notice Calculates the total fee required for a price update request
     * @dev Total fee = base Pyth protocol fee + base provider fee + provider fee per feed + gas costs for callback
     * @param provider The provider to fulfill the request
     * @param callbackGasLimit The amount of gas allocated for callback execution
     * @param priceIds The price feed IDs to update.
     * @return feeAmount The total fee in wei that must be provided as msg.value
     */
    function getFee(
        address provider,
        uint32 callbackGasLimit,
        bytes32[] calldata priceIds
    ) external view returns (uint96 feeAmount);

    function getAccruedPythFees()
        external
        view
        returns (uint128 accruedFeesInWei);

    function getRequest(
        uint64 sequenceNumber
    ) external view returns (EchoState.Request memory req);

    function setFeeManager(address manager) external;

    /**
     * @notice Allows the admin to withdraw accumulated Pyth protocol fees
     * @param amount The amount of fees to withdraw in wei
     */
    function withdrawFees(uint128 amount) external;

    function withdrawAsFeeManager(address provider, uint128 amount) external;

    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
        uint96 feePerGasInWei
    ) external;

    function setProviderFee(
        address provider,
        uint96 newBaseFeeInWei,
        uint96 newFeePerFeedInWei,
        uint96 newFeePerGasInWei
    ) external;

    function getProviderInfo(
        address provider
    ) external view returns (EchoState.ProviderInfo memory);

    function getDefaultProvider() external view returns (address);

    function setDefaultProvider(address provider) external;

    function setExclusivityPeriod(uint32 periodSeconds) external;

    function getExclusivityPeriod() external view returns (uint32);

```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L12-29)
```text
    struct Request {
        // Slot 1: 8 + 8 + 4 + 12 = 32 bytes
        uint64 sequenceNumber;
        uint64 publishTime;
        uint32 callbackGasLimit;
        uint96 fee;
        // Slot 2: 20 + 12 = 32 bytes
        address requester;
        // 12 bytes padding

        // Slot 3: 20 + 12 = 32 bytes
        address provider;
        // 12 bytes padding

        // Dynamic array starts at its own slot
        // Store only first 8 bytes of each price ID to save gas
        bytes8[] priceIdPrefixes;
    }
```

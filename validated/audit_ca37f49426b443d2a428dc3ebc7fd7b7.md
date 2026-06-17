### Title
Missing Deadline Check in `requestPriceUpdatesWithCallback` Allows Stale Price Data Delivery to Callbacks — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo.sol` contract's `requestPriceUpdatesWithCallback` function accepts a user-supplied `publishTime` (typically set to `block.timestamp` at signing time) but provides no `deadline` parameter. If the transaction is delayed in the mempool, the stored `publishTime` becomes stale. The provider then fulfills the request with price data from that past time, and the user's callback receives outdated prices with no ability to prevent it.

---

### Finding Description

`requestPriceUpdatesWithCallback` stores the caller-supplied `publishTime` directly into the request struct:

```solidity
function requestPriceUpdatesWithCallback(
    address provider,
    uint64 publishTime,
    bytes32[] calldata priceIds,
    uint32 callbackGasLimit
) external payable override returns (uint64 requestSequenceNumber) {
    require(publishTime <= block.timestamp + 60, "Too far in future");
    ...
    req.publishTime = publishTime;
``` [1](#0-0) 

The only temporal guard is an upper bound (`publishTime <= block.timestamp + 60`). There is no lower bound and no deadline parameter. When `executeCallback` is later called, it passes `req.publishTime` as **both** `minPublishTime` and `maxPublishTime` to `parsePriceFeedUpdates`:

```solidity
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{value: pythFee}(
    updateData,
    priceIds,
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)
);
``` [2](#0-1) 

This means the provider is forced to supply price data whose `publishTime` exactly matches the value the user set at signing time. If the transaction was delayed by hours or days, the callback receives price data from that stale point in time.

There is no expiry on the stored request either — `executeCallback` has no check that the request has not aged beyond a reasonable window: [3](#0-2) 

---

### Impact Explanation

Any consumer contract whose `echoCallback` uses the delivered price feeds to make financial decisions (e.g., trigger a trade, check collateral sufficiency, execute a liquidation) will act on prices that may be hours or days old. The user has no on-chain mechanism to abort the request if it was not executed promptly. This is directly analogous to the Portal.sol `convert()` / `buyPortalEnergy()` / `sellPortalEnergy()` missing-deadline class: the user's intent (get current prices) is silently violated by mempool delay. [4](#0-3) 

---

### Likelihood Explanation

Medium. Mempool delays are routine on EVM chains during congestion. A user who submits with a below-market gas price can have their transaction pending for hours or days. The `publishTime <= block.timestamp + 60` guard only prevents far-future requests; it does not prevent a stale-at-execution request. No special attacker capability is required — natural network conditions are sufficient.

---

### Recommendation

Add a `deadline` parameter to `requestPriceUpdatesWithCallback` and revert if the transaction executes after it:

```diff
function requestPriceUpdatesWithCallback(
    address provider,
    uint64 publishTime,
    bytes32[] calldata priceIds,
-   uint32 callbackGasLimit
+   uint32 callbackGasLimit,
+   uint64 deadline
) external payable override returns (uint64 requestSequenceNumber) {
+   if (block.timestamp > deadline) revert DeadlineExpired();
    require(publishTime <= block.timestamp + 60, "Too far in future");
    ...
}
``` [5](#0-4) 

---

### Proof of Concept

1. Alice wants current ETH/USD prices. She signs a transaction calling `requestPriceUpdatesWithCallback(provider, block.timestamp /*= T₀*/, [ETH_USD_ID], 100_000)` with a low gas price.
2. The transaction sits in the mempool for 3 hours. At execution time `block.timestamp = T₀ + 10800`.
3. The guard `publishTime <= block.timestamp + 60` passes trivially (`T₀ <= T₀ + 10860`).
4. The request is stored with `req.publishTime = T₀`.
5. The provider calls `executeCallback`, supplying Pyth update data for time `T₀`. `parsePriceFeedUpdates` is called with `minPublishTime = maxPublishTime = T₀`, so only 3-hour-old price data is accepted.
6. Alice's `echoCallback` fires with prices from 3 hours ago.
7. If Alice's contract uses those prices to decide whether to liquidate a position or execute a swap, it acts on stale data — potentially to her financial detriment. [6](#0-5) [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-121)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L143-153)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L176-201)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L54-59)
```text
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable returns (uint64 sequenceNumber);
```

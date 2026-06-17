### Title
Missing Refund/Cancel Mechanism for Unfulfilled Requests Permanently Locks User Fees — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo.sol` contract accepts user-paid fees in `requestPriceUpdatesWithCallback` and stores them in a per-request struct. There is no expiry, cancellation, or refund path for users. If the assigned provider never calls `executeCallback` — due to downtime, chain congestion, or deliberate non-fulfillment — the user's fee is permanently locked in the contract with no recovery path.

---

### Finding Description

When a user calls `requestPriceUpdatesWithCallback`, the fee they pay (minus the Pyth protocol fee) is stored directly in the `Request` struct:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [1](#0-0) 

The Pyth protocol portion is immediately accrued:

```solidity
_state.accruedFeesInWei += _state.pythFeeInWei;
``` [2](#0-1) 

The provider's portion (`req.fee`) is only released when `executeCallback` is successfully called:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [3](#0-2) 

`executeCallback` also calls `parsePriceFeedUpdates` with **both** `minPublishTime` and `maxPublishTime` set to the exact `req.publishTime`:

```solidity
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
    value: pythFee
}(
    updateData,
    priceIds,
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)
);
``` [4](#0-3) 

This means the price update submitted must have a publish timestamp **exactly equal** to `req.publishTime`. If the provider cannot source a Pyth update at that exact second (e.g., due to chain downtime, Pyth publishing gaps, or network delays), `executeCallback` will revert and the request can never be fulfilled.

The contract itself acknowledges the problem in a developer TODO:

```solidity
// TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
// If executeCallback can revert, then funds can be permanently locked in the contract.
// TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
``` [5](#0-4) 

Searching the entire `IEcho` interface and `Echo.sol` implementation confirms there is **no** `cancelRequest`, `refundRequest`, or any user-callable function to recover funds from an unfulfilled request. [6](#0-5) 

---

### Impact Explanation

Any user who calls `requestPriceUpdatesWithCallback` and pays a fee faces permanent loss of those funds if the provider fails to fulfill. The ETH sits in the contract with no mechanism to retrieve it. This is a direct loss of user funds — the highest-severity class of smart contract vulnerability.

---

### Likelihood Explanation

The scenario is realistic and reachable without any privileged access:

1. **Provider downtime**: A registered provider goes offline after users have submitted requests. The exclusivity period passes, but no other provider has incentive to fulfill at a loss (they would need to pay the Pyth fee out of pocket via `msg.value`).
2. **Exact-timestamp impossibility**: The user sets `publishTime` to a specific second. If Pyth did not publish a price update at that exact second (e.g., due to a brief outage), no valid `updateData` exists for that timestamp, making `executeCallback` permanently unfulfillable.
3. **Malicious provider**: A provider registers, attracts requests, and deliberately never fulfills them. The `pythFeeInWei` portion is already accrued to the protocol; the provider loses nothing by not fulfilling.

Any unprivileged user can trigger this by calling `requestPriceUpdatesWithCallback` — no special role is required. [7](#0-6) 

---

### Recommendation

1. **Add a request expiry and user-callable refund**: After a configurable deadline (e.g., `publishTime + MAX_FULFILLMENT_DELAY`), allow the original requester to call a `cancelRequest(uint64 sequenceNumber)` function that returns `req.fee` to `req.requester`.
2. **Relax the exact-timestamp constraint**: Change `parsePriceFeedUpdates` to use `[req.publishTime, req.publishTime + TOLERANCE]` instead of an exact match, so minor timestamp drift does not permanently block fulfillment.
3. **Penalize non-fulfilling providers**: Slash a portion of the provider's staked/accrued balance if they fail to fulfill within the deadline, and redirect it to the user as compensation.

---

### Proof of Concept

1. Deploy `EchoUpgradeable` with a registered provider.
2. User calls `requestPriceUpdatesWithCallback{value: fee}(provider, block.timestamp, priceIds, gasLimit)`.
3. Provider goes offline (or no Pyth update exists at exactly `block.timestamp`).
4. After any amount of time, the user has no function to call to recover `req.fee`.
5. `getRequest(sequenceNumber)` still shows the request as active with `req.fee > 0`, but no path exists to return those funds to the user. [8](#0-7)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L146-153)
```text
        PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
            value: pythFee
        }(
            updateData,
            priceIds,
            SafeCast.toUint64(req.publishTime),
            SafeCast.toUint64(req.publishTime)
        );
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-159)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L301-321)
```text
    function findActiveRequest(
        uint64 sequenceNumber
    ) internal view returns (Request storage req) {
        req = findRequest(sequenceNumber);

        if (!isActive(req) || req.sequenceNumber != sequenceNumber)
            revert NoSuchRequest();
    }

    function findRequest(
        uint64 sequenceNumber
    ) internal view returns (Request storage req) {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            return req;
        } else {
            req = _state.requestsOverflow[key];
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L37-161)
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

    /**
     * @notice Gets the first N active requests
     * @param count Maximum number of active requests to return
     * @return requests Array of active requests, ordered from oldest to newest
     * @return actualCount Number of active requests found (may be less than count)
     * @dev Gas Usage: This function's gas cost scales linearly with the number of requests
     *      between firstUnfulfilledSeq and currentSequenceNumber. Each iteration costs approximately:
     *      - 2100 gas for cold storage reads, 100 gas for warm storage reads (SLOAD)
     *      - Additional gas for array operations
     *      The function starts from firstUnfulfilledSeq (all requests before this are fulfilled)
     *      and scans forward until it finds enough active requests or reaches currentSequenceNumber.
     */
    function getFirstActiveRequests(
        uint256 count
    )
        external
        view
        returns (EchoState.Request[] memory requests, uint256 actualCount);
}
```

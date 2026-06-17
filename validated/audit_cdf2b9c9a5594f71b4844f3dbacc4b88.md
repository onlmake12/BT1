### Title
User Fee Permanently Locked When Echo Request Cannot Be Fulfilled - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
In `Echo.sol`, the provider fee paid by a requester is stored in the `Request` struct at request time and is only credited to the provider when `executeCallback` succeeds. Because `executeCallback` requires a Pyth price update at an **exact** `publishTime` (min == max), any request for a timestamp with no matching Pyth update becomes permanently unfulfillable. There is no cancellation, expiration, or refund mechanism, so the locked fee is irrecoverable.

### Finding Description

When a user calls `requestPriceUpdatesWithCallback`, the fee is split immediately: the Pyth protocol portion is credited to `_state.accruedFeesInWei`, while the provider portion is stored in `req.fee`: [1](#0-0) 

The provider fee is only released inside `executeCallback`, which calls `parsePriceFeedUpdates` with `minPublishTime = maxPublishTime = req.publishTime`: [2](#0-1) 

This exact-timestamp constraint means that if no Pyth price update was published at precisely `req.publishTime`, `parsePriceFeedUpdates` will revert and `executeCallback` can never succeed. The contract itself acknowledges this in a TODO comment: [3](#0-2) 

There is no `cancelRequest`, `refundFee`, or expiration path anywhere in `Echo.sol` or `IEcho.sol`: [4](#0-3) 

The `Request` struct stores `fee` as `uint96` and the only way it is ever consumed is through `clearRequest` inside `executeCallback`: [5](#0-4) 

### Impact Explanation

Any ETH paid as the provider fee (`msg.value - pythFeeInWei`) for a request that cannot be fulfilled is permanently locked in the Echo contract. The contract holds the ETH but has no code path to return it to the requester or credit it to any party. At scale, this drains user funds with no recourse.

### Likelihood Explanation

Pyth publishes price updates approximately every 400ms. A user who specifies `publishTime = block.timestamp` (a common pattern) will frequently land on a second for which no exact Pyth update exists. Additionally, if the assigned provider goes offline after the exclusivity period, no other party is economically incentivized to fulfill the request (they would have to supply their own `msg.value` for the Pyth fee at line 145 with no guarantee of reimbursement beyond `req.fee`). Both conditions are reachable by any unprivileged user without any special access.

### Recommendation

- **Short term:** Add a `cancelRequest(uint64 sequenceNumber)` function that allows the original requester to reclaim `req.fee` after a configurable timeout (e.g., after the exclusivity period has elapsed with no fulfillment). Alternatively, relax the exact-timestamp constraint in `executeCallback` to accept a price update within a small window around `req.publishTime`.
- **Long term:** Model and document all request lifecycle states (pending, fulfilled, expired, cancelled) and implement test cases covering each transition, including the case where no price data exists at the requested timestamp.

### Proof of Concept

1. Alice calls `requestPriceUpdatesWithCallback(provider, block.timestamp, priceIds, gasLimit)` sending `fee = 0.01 ETH`. `req.fee = 0.01 ETH - pythFeeInWei` is stored in the request.
2. No Pyth price update was published at exactly `block.timestamp` (common, given ~400ms update cadence).
3. Any call to `executeCallback(..., sequenceNumber, updateData, priceIds)` reverts inside `parsePriceFeedUpdates` because no update satisfies `minPublishTime == maxPublishTime == req.publishTime`.
4. The exclusivity period passes; the assigned provider is no longer required to fulfill.
5. No other party can fulfill either, because the exact-timestamp constraint is protocol-enforced.
6. Alice's `req.fee` ETH is permanently locked in the Echo contract with no recovery path.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L84-99)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L143-162)
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

        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
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

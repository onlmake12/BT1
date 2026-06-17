### Title
Missing Request Cancellation Mechanism Permanently Locks User Fees — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo` contract accepts ETH fees from users when they call `requestPriceUpdatesWithCallback`, storing the provider-fee portion inside the request struct (`req.fee`). However, neither `Echo.sol` nor `IEcho.sol` implements any `cancelRequest` or refund function. If the assigned provider never calls `executeCallback`, the user's ETH is permanently locked in the contract with no recovery path.

---

### Finding Description

When a user calls `requestPriceUpdatesWithCallback`, the contract splits the incoming ETH into two parts:

1. The Pyth protocol fee, immediately credited to `_state.accruedFeesInWei`: [1](#0-0) 

2. The provider fee, stored inside the request struct: [2](#0-1) 

The provider fee (`req.fee`) is only ever credited to the provider when `executeCallback` is successfully called: [3](#0-2) 

The full `IEcho` interface exposes no `cancelRequest`, `cancelAndRefund`, or equivalent function: [4](#0-3) 

The contract itself contains a developer TODO that acknowledges the locked-funds risk but provides no mitigation: [5](#0-4) 

Additionally, during the exclusivity period, only the originally assigned provider may call `executeCallback`: [6](#0-5) 

This means that if the assigned provider is offline or unresponsive during the exclusivity window, no other party can fulfill the request, and the user cannot cancel it either.

---

### Impact Explanation

The provider fee portion of every unfulfilled request (`req.fee`) is permanently locked in the `Echo` contract. The Pyth fee portion is already credited to `_state.accruedFeesInWei` and can be withdrawn by the admin, but the provider fee has no withdrawal path unless `executeCallback` is called. Users who pay fees for price update requests that are never fulfilled lose their ETH with no recourse.

---

### Likelihood Explanation

Any registered provider can go offline, become unresponsive, or deliberately refuse to fulfill requests. The exclusivity period enforced by `_state.exclusivityPeriodSeconds` (default 15 seconds per tests) means that during that window, no alternative provider can step in. After the exclusivity period, any provider *can* fulfill, but there is no economic incentive to fulfill a stale request. In practice, if the assigned provider fails and no other provider fulfills, the funds are locked indefinitely.

---

### Recommendation

Implement a `cancelRequest(uint64 sequenceNumber)` function that:
1. Verifies `msg.sender == req.requester`.
2. Optionally enforces a minimum waiting period (e.g., after the exclusivity period has elapsed) to prevent griefing.
3. Refunds `req.fee` to the requester.
4. Calls `clearRequest(sequenceNumber)` to clean up storage.

The Pyth fee (`_state.pythFeeInWei`) may be retained as a non-refundable service fee, consistent with the existing design where it is immediately credited on request creation.

---

### Proof of Concept

1. User calls `echo.requestPriceUpdatesWithCallback{value: totalFee}(provider, publishTime, priceIds, gasLimit)`.
2. Contract stores `req.fee = totalFee - pythFeeInWei` and credits `_state.accruedFeesInWei += pythFeeInWei`.
3. The assigned `provider` goes offline and never calls `executeCallback`.
4. After the exclusivity period, no other provider calls `executeCallback` (no incentive for a stale request).
5. User attempts to recover funds — no `cancelRequest` function exists in `IEcho` or `Echo`.
6. `req.fee` remains locked in the contract indefinitely. [7](#0-6) [8](#0-7)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L114-121)
```text
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }
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

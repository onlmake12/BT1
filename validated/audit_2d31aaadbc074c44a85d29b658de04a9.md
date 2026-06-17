### Title
User Funds Permanently Locked in Echo Contract When Provider Fails to Fulfill Request — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo` contract holds the provider fee portion of a user's payment (`req.fee`) in contract storage until the provider calls `executeCallback`. There is no `cancelRequest` or user-initiated refund path. If the assigned provider never fulfills the request, the user's funds are permanently locked with no recovery mechanism.

---

### Finding Description

When a user calls `requestPriceUpdatesWithCallback`, the total `msg.value` is split:

- `_state.pythFeeInWei` is immediately credited to Pyth's accrued fees
- The remainder is stored in `req.fee` — the provider's portion — held in the contract pending fulfillment [1](#0-0) 

The provider fee is **not** immediately credited to the provider. It sits in the `Request` struct until `executeCallback` is called by the provider (or anyone, after the exclusivity window). [2](#0-1) 

During the exclusivity period (`block.timestamp < req.publishTime + exclusivityPeriodSeconds`), only the originally assigned provider may call `executeCallback`. [3](#0-2) 

After the exclusivity period, anyone may call `executeCallback`. However, this requires the caller to supply valid Pyth `updateData` for the correct `publishTime` and `priceIds`. An ordinary user who simply wants their fee back cannot do this without the off-chain data.

The `IEcho` interface exposes no `cancelRequest`, `refundRequest`, or equivalent function for users. [4](#0-3) 

The `clearRequest` internal function only zeroes the storage slot; it does not transfer any funds. [5](#0-4) 

---

### Impact Explanation

A user who calls `requestPriceUpdatesWithCallback` against a malicious or non-functional provider loses the provider-fee portion of their payment permanently. The funds accumulate in the contract with no on-chain path for the user to recover them. The provider's NFT-equivalent (their registered status and accrued fees from other users) is unaffected, so the provider has no economic incentive to fulfill.

Severity: **Medium** — direct, permanent loss of user funds with no recovery path.

---

### Likelihood Explanation

Provider registration is fully permissionless via `registerProvider`. [6](#0-5) 

Any unprivileged address can register, attract users (e.g., by advertising a low fee), collect requests, and then simply never call `executeCallback`. The exclusivity period initially blocks other providers from stepping in, giving the malicious provider a window of guaranteed non-fulfillment. After the exclusivity period, fulfillment requires valid off-chain Pyth update data that the average user does not possess.

---

### Recommendation

Add a user-callable `cancelRequest` function that:
1. Can only be called after a timeout (e.g., `block.timestamp > req.publishTime + exclusivityPeriodSeconds + GRACE_PERIOD`)
2. Refunds `req.fee` to `req.requester`
3. Clears the request from storage

```solidity
function cancelRequest(uint64 sequenceNumber) external {
    Request storage req = findActiveRequest(sequenceNumber);
    require(msg.sender == req.requester, "Only requester");
    require(
        block.timestamp > req.publishTime + _state.exclusivityPeriodSeconds + CANCEL_GRACE_PERIOD,
        "Too early to cancel"
    );
    uint96 refund = req.fee;
    clearRequest(sequenceNumber);
    (bool sent, ) = req.requester.call{value: refund}("");
    require(sent, "Refund failed");
}
```

---

### Proof of Concept

1. Attacker calls `registerProvider(lowFee, lowFee, lowFee)` — permissionless, no stake required.
2. Victim calls `requestPriceUpdatesWithCallback{value: fee}(attacker, publishTime, priceIds, gasLimit)`.
   - `req.fee = msg.value - pythFeeInWei` is stored in the request struct.
3. Attacker does nothing. During `exclusivityPeriodSeconds`, no other provider can fulfill.
4. After the exclusivity period, fulfillment requires valid Pyth `updateData` — the victim does not have this.
5. `req.fee` remains locked in the contract indefinitely. The victim has no on-chain path to recover it. [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L334-344)
```text
    function allocRequest(
        uint64 sequenceNumber
    ) internal returns (Request storage req) {
        (, uint8 shortKey) = requestKey(sequenceNumber);

        req = _state.requests[shortKey];
        if (isActive(req)) {
            (bytes32 reqKey, ) = requestKey(req.sequenceNumber);
            _state.requestsOverflow[reqKey] = req;
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-393)
```text
    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
        uint96 feePerGasInWei
    ) external override {
        ProviderInfo storage provider = _state.providers[msg.sender];
        require(!provider.isRegistered, "Provider already registered");
        provider.baseFeeInWei = baseFeeInWei;
        provider.feePerFeedInWei = feePerFeedInWei;
        provider.feePerGasInWei = feePerGasInWei;
        provider.isRegistered = true;
        emit ProviderRegistered(msg.sender, feePerGasInWei);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L37-144)
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
```

### Title
Missing Cancel/Refund Mechanism Permanently Locks User Funds When Provider Fails to Fulfill - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo` contract accepts user fee payments upfront in `requestPriceUpdatesWithCallback()` but provides no mechanism for users to cancel a pending request and recover their locked provider fee if the assigned provider never calls `executeCallback()`. The provider fee portion is held in the contract until fulfillment, and with no cancel function, those funds are permanently locked.

---

### Finding Description

When a user calls `requestPriceUpdatesWithCallback()`, the total `msg.value` is split into two parts:

1. The Pyth protocol fee (`_state.pythFeeInWei`) is immediately credited to `_state.accruedFeesInWei`.
2. The provider fee (`msg.value - _state.pythFeeInWei`) is stored in `req.fee` inside the request struct and held in the contract. [1](#0-0) 

The provider fee is only released to the provider when `executeCallback()` is successfully called: [2](#0-1) 

If the provider never calls `executeCallback()`, the `req.fee` amount remains locked in the contract indefinitely. There is no `cancelRequest`, `refundRequest`, or any equivalent function anywhere in the Echo contract or its interface. [3](#0-2) 

The codebase itself acknowledges this gap with explicit TODO comments: [4](#0-3) 

The `Request` struct stores the locked fee: [5](#0-4) 

---

### Impact Explanation

A user who calls `requestPriceUpdatesWithCallback()` and pays the required fee has their provider fee portion (`req.fee`) permanently locked in the Echo contract if the provider never fulfills. The user cannot recover these funds. There is no timeout-based refund, no user-initiated cancel, and no admin recovery path for individual user funds. The only admin withdrawal function (`withdrawFees`) targets the Pyth protocol's own accrued fees, not user-locked request fees. [6](#0-5) 

**Impact**: Permanent loss of user funds (the provider fee portion of every unfulfilled request).

---

### Likelihood Explanation

The assigned provider may fail to fulfill for several realistic reasons:

- The provider goes offline or is decommissioned after the user's request is submitted.
- The provider's off-chain infrastructure fails to observe the `PriceUpdateRequested` event.
- The provider deliberately withholds fulfillment (griefing or censorship).
- During the exclusivity period (`exclusivityPeriodSeconds`), only the assigned provider may call `executeCallback()`, so no third party can rescue the request during that window. [7](#0-6) 

After the exclusivity period, any party may fulfill, but there is still no user-initiated path to cancel and recover funds if no one fulfills. The entry path is fully unprivileged: any user calling `requestPriceUpdatesWithCallback()` is exposed.

---

### Recommendation

Implement a `cancelRequest(uint64 sequenceNumber)` function that:

1. Verifies `msg.sender == req.requester`.
2. Enforces a minimum waiting period (e.g., after the exclusivity period has elapsed) to prevent abuse.
3. Refunds `req.fee` to the requester.
4. Calls `clearRequest(sequenceNumber)`.

Additionally, consider a time-based automatic refund path or a penalty mechanism that transfers the provider fee to the requester if the provider fails to fulfill within a defined deadline, consistent with the existing TODO comment at line 157. [8](#0-7) 

---

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback{value: fee}(provider, publishTime, priceIds, gasLimit)`.
2. `req.fee = msg.value - _state.pythFeeInWei` is stored in the request; only the Pyth fee is credited immediately.
3. The assigned provider goes offline and never calls `executeCallback()`.
4. The exclusivity period elapses; no third party fulfills either.
5. The user attempts to recover funds — no cancel or refund function exists.
6. `req.fee` remains locked in the contract with no recovery path. [9](#0-8)

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

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L37-75)
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

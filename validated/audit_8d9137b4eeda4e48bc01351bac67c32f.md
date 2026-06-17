### Title
Permanently Locked User ETH in `Echo.sol` Due to Missing Request Cancellation Mechanism — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
The `Echo` contract accepts ETH from users via `requestPriceUpdatesWithCallback`, splits it between a Pyth protocol fee (immediately credited to `accruedFeesInWei`) and a provider fee (stored in `req.fee` inside the request struct). However, there is no `cancelRequest` or timeout-based refund function. If a request is never fulfilled — because a provider goes offline, stops operating, or simply ignores the request — the provider-fee portion of the user's ETH (`req.fee`) is permanently locked in the contract with no recovery path for the user or the admin.

### Finding Description
In `Echo.sol`, `requestPriceUpdatesWithCallback` is `payable` and splits `msg.value` as follows: [1](#0-0) 

- `_state.pythFeeInWei` is immediately credited to `_state.accruedFeesInWei` (withdrawable by admin).
- The remainder, `msg.value - _state.pythFeeInWei`, is stored as `req.fee` inside the request struct. [2](#0-1) 

The only function that ever clears a request and disburses `req.fee` is `executeCallback`, which calls the internal `clearRequest`: [3](#0-2) 

There is no `cancelRequest`, no timeout-based expiry, and no admin sweep for funds stored inside individual request structs. The `withdrawFees` function only covers `_state.accruedFeesInWei` (the Pyth portion): [4](#0-3) 

Additionally, the `executeCallback` function itself is marked `payable` with an explicit TODO comment questioning whether this is necessary: [5](#0-4) 

Any ETH sent to `executeCallback` by a keeper is credited to the provider's accrued fees rather than refunded to the caller, compounding the misdirected-funds surface.

### Impact Explanation
If a provider registered in `Echo` stops fulfilling requests (goes offline, is deregistered, or is malicious), all in-flight requests assigned to that provider have their `req.fee` permanently locked in the contract. The user cannot cancel the request, cannot time it out, and cannot recover the ETH. The admin can only recover `accruedFeesInWei` (the Pyth protocol portion), not the per-request provider fees. This results in a direct, permanent loss of user funds proportional to the number of unfulfilled requests.

### Likelihood Explanation
Any permissionlessly registered provider can stop fulfilling callbacks at any time. The `registerProvider` function has no stake or bond requirement: [6](#0-5) 

A malicious or simply inactive provider can cause all users who requested from them to permanently lose their `req.fee`. The exclusivity period further delays any alternative fulfillment: [7](#0-6) 

During the exclusivity window, no other provider can fulfill the request, so even a brief provider outage during this window locks funds.

### Recommendation
1. Add a `cancelRequest(uint64 sequenceNumber)` function that allows the original requester to reclaim `req.fee` after a configurable timeout (e.g., after `publishTime + exclusivityPeriod + buffer` has elapsed).
2. Remove the `payable` modifier from `executeCallback` since it is not required (per the existing TODO comment) and creates a misdirected-funds surface.
3. Consider requiring providers to post a bond on registration, slashable if they fail to fulfill requests within a deadline.

### Proof of Concept
1. Alice calls `requestPriceUpdatesWithCallback{value: 1 ether}(maliciousProvider, ...)`.
2. `_state.accruedFeesInWei += pythFeeInWei` (e.g., 0.01 ETH credited to Pyth admin).
3. `req.fee = 0.99 ETH` stored in the request struct.
4. `maliciousProvider` never calls `executeCallback`.
5. Alice has no function to call to recover her 0.99 ETH.
6. Admin calls `withdrawFees(0.01 ether)` — only the Pyth portion is recoverable.
7. Alice's 0.99 ETH is permanently locked in the contract. [8](#0-7)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-110)
```text
    // TODO: does this need to be payable? Any cost paid to Pyth could be taken out of the provider's accrued fees.
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-164)
```text
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

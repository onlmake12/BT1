### Title
Echo.sol `executeCallback` Provider Fee Frontrunning After Exclusivity Period — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
`Echo.sol`'s `executeCallback` function accepts a caller-controlled `providerToCredit` parameter. After the exclusivity period expires, there is no restriction on who can be named as `providerToCredit`. An attacker can monitor the mempool for a legitimate provider's `executeCallback` transaction, copy the `updateData` and `priceIds` arguments, and submit a frontrunning transaction naming themselves as `providerToCredit`, stealing the provider fee stored in `req.fee`.

### Finding Description
When a user calls `requestPriceUpdatesWithCallback`, the fee they pay is split: the Pyth protocol fee is immediately credited to `_state.accruedFeesInWei`, and the remainder is stored in `req.fee` to be credited to the fulfilling provider upon callback execution. [1](#0-0) 

The `executeCallback` function enforces an exclusivity period during which only `req.provider` may be named as `providerToCredit`: [2](#0-1) 

Once `block.timestamp >= req.publishTime + _state.exclusivityPeriodSeconds` (default 15 seconds), this guard is bypassed entirely. Any caller may pass an arbitrary address as `providerToCredit`. Because `req.fee` is credited to `providerToCredit`, an attacker who has pre-registered as a provider (registration is permissionless via `registerProvider`) can:

1. Watch the mempool for a legitimate provider's `executeCallback` call.
2. Copy `sequenceNumber`, `updateData`, and `priceIds` verbatim.
3. Submit the same call with their own address as `providerToCredit` at a higher gas price.
4. Have `req.fee` credited to their provider account.
5. Withdraw the stolen fees via the provider withdrawal path.

The `executeCallback` function signature and the exclusivity-period guard confirm the two-step pattern: fee is deposited in step 1 (`requestPriceUpdatesWithCallback`) and claimed in step 2 (`executeCallback`), with step 2 open to any caller after the exclusivity window. [3](#0-2) 

### Impact Explanation
The legitimate provider loses the fee they were owed for fulfilling the price update request. The attacker receives `req.fee` (provider base fee + per-feed fee + gas fee component) for every request they successfully frontrun. Over time this drains provider revenue, disincentivizes honest providers from operating, and degrades the liveness of the Echo price-update service.

### Likelihood Explanation
The exclusivity period is only 15 seconds by default. On chains with public mempools (Ethereum mainnet, most L2s), an attacker can reliably detect and frontrun `executeCallback` transactions. The attacker needs no special privilege — provider registration is permissionless. The `updateData` payload required to call `executeCallback` is fully visible in the pending transaction, so the attacker incurs no additional cost to construct the frontrunning call.

### Recommendation
Remove the `providerToCredit` parameter from `executeCallback`. Instead, credit `req.provider` unconditionally (as is already enforced during the exclusivity period). If post-exclusivity open fulfillment is desired, credit the `msg.sender` only when they are a registered provider, and verify that `msg.sender` is the same address that submitted the valid `updateData` — or, alternatively, credit `req.provider` regardless of who calls `executeCallback`, since the provider is the party that committed to fulfilling the request.

### Proof of Concept
1. User calls `requestPriceUpdatesWithCallback{value: fee}(defaultProvider, publishTime, priceIds, gasLimit)` — `req.fee = fee - pythFee` is stored, `req.provider = defaultProvider`.
2. 16 seconds pass (exclusivity period of 15 s expires).
3. `defaultProvider` submits `executeCallback(defaultProvider, seqNum, updateData, priceIds)` to the mempool.
4. Attacker sees the pending transaction, registers as a provider (permissionless), and submits `executeCallback(attackerAddress, seqNum, updateData, priceIds)` with higher gas.
5. Attacker's transaction is mined first. `req.fee` is credited to `attackerAddress`.
6. `defaultProvider`'s transaction reverts with `NoSuchRequest` (request already cleared).
7. Attacker calls the provider fee withdrawal function and receives `req.fee`. [4](#0-3) [3](#0-2)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-120)
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
```

### Title
Front-Running `executeCallback` After Exclusivity Period Enables Provider Fee Theft - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
After the exclusivity window expires, `Echo.executeCallback()` accepts an attacker-controlled `providerToCredit` address with no further restriction. Any unprivileged actor can front-run the legitimate provider's callback submission, redirect the stored fee to themselves, and cause the provider's transaction to revert.

### Finding Description
`Echo.requestPriceUpdatesWithCallback()` stores a fee (`req.fee`) and assigns `req.provider` at request time. The `executeCallback()` function enforces provider exclusivity only while `block.timestamp < req.publishTime + exclusivityPeriodSeconds`:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
```

After that window, the check is skipped entirely and `providerToCredit` is fully attacker-controlled. An attacker monitoring the mempool can observe the legitimate provider's pending `executeCallback(provider, seqNum, updateData, priceIds)` transaction, copy `updateData` and `priceIds` verbatim, substitute their own address as `providerToCredit`, and submit with a higher gas price. The attacker's transaction clears the request and credits the fee to the attacker; the provider's transaction then reverts with `NoSuchRequest`.

The root cause is that the design correctly separates *who may call* (anyone, for liveness) from *who should be credited* (always the assigned provider), but the implementation conflates the two by making `providerToCredit` a free caller-supplied parameter after the exclusivity period. [1](#0-0) 

The fee is stored in `req.fee` at request time and is meant to compensate the provider for off-chain work: [2](#0-1) 

The `Request` struct confirms `fee` and `provider` are both stored per-request: [3](#0-2) 

### Impact Explanation
The legitimate provider performs all off-chain work (fetching price data, constructing `updateData`, paying gas) but receives zero fee. An MEV bot can systematically steal every provider fee on any chain where Echo is deployed, making it economically unviable for providers to operate and degrading the liveness of the price-update callback service.

### Likelihood Explanation
Front-running via mempool observation is a well-established MEV technique on all EVM chains. The attacker needs only to copy the provider's calldata and replace one address field. No special privilege, leaked key, or oracle manipulation is required. The attack is profitable on every request where `req.fee > gas_cost_of_front_run`.

### Recommendation
Remove the `providerToCredit` parameter from `executeCallback` and always credit `req.provider` unconditionally. If the protocol intends to allow third-party relayers to earn a tip, introduce a separate, bounded relayer-tip field at request time rather than allowing the caller to redirect the entire provider fee.

```solidity
// Instead of:
function executeCallback(address providerToCredit, uint64 sequenceNumber, ...)

// Use:
function executeCallback(uint64 sequenceNumber, ...) {
    // always credit req.provider
}
```

### Proof of Concept
1. User calls `requestPriceUpdatesWithCallback(provider, publishTime, priceIds, gasLimit)` with `msg.value = fee`. Contract stores `req.provider = provider`, `req.fee = fee - pythFee`.
2. `exclusivityPeriodSeconds` elapses past `req.publishTime`.
3. Legitimate provider broadcasts `executeCallback(provider, seqNum, updateData, priceIds)`.
4. Attacker sees the pending transaction in the mempool, copies `seqNum`, `updateData`, `priceIds`, and submits `executeCallback(attacker, seqNum, updateData, priceIds)` with a higher gas price.
5. Attacker's transaction is mined first: request is cleared, `req.fee` is credited to `attacker`.
6. Provider's transaction reverts (`NoSuchRequest`). Provider receives nothing despite having done all the work. [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L73-101)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-160)
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

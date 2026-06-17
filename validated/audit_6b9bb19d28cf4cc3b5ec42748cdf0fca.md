### Title
Missing Price Feed ID Validation in `requestPriceUpdatesWithCallback` Allows Permanent Fee Locking - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo.sol` contract's `requestPriceUpdatesWithCallback` function accepts arbitrary `priceIds` without validating that they correspond to price feeds actually supported by the Pyth oracle. If a user submits an unsupported or non-existent price feed ID, the paid fee is permanently locked in the contract with no refund or cancellation mechanism.

---

### Finding Description

`requestPriceUpdatesWithCallback` in `Echo.sol` performs the following checks before accepting a request and fee:

1. Provider is registered
2. `publishTime` is not more than 60 seconds in the future
3. `priceIds.length <= MAX_PRICE_IDS`
4. `msg.value >= requiredFee` [1](#0-0) 

There is **no validation** that the submitted `priceIds` are valid, supported Pyth price feed IDs. The fee is immediately split: the Pyth protocol fee is accrued to `_state.accruedFeesInWei`, and the provider fee is stored in `req.fee`. [2](#0-1) 

When the provider later attempts to fulfill the request via `executeCallback`, it calls `pyth.parsePriceFeedUpdates` with the stored `priceIds`. If the price ID does not exist in the Pyth oracle, this call reverts — the provider cannot supply valid `updateData` for a non-existent feed. The request remains permanently active with no path to refund or cancel. [3](#0-2) 

The contract's own inline TODO comment acknowledges this exact risk: [4](#0-3) 

> "if this effect occurs here, we need to guarantee that executeCallback can never revert. If executeCallback can revert, then funds can be permanently locked in the contract."

Additionally, the price ID verification at callback time only compares the **first 8 bytes** (prefix) of each price ID, not the full 32-byte identifier: [5](#0-4) 

This weak check means a user could submit a price ID whose first 8 bytes match a valid feed but whose remaining 24 bytes are arbitrary — the provider would be forced to fulfill with a different price ID than the user intended, or the request remains stuck.

---

### Impact Explanation

A user who submits an invalid or unsupported `priceId` (e.g., due to a buggy frontend, typo, or a price feed that was recently deprecated) will:

1. Lose the Pyth protocol fee (`pythFeeInWei`) immediately — it is accrued to the contract.
2. Have the provider fee (`req.fee`) permanently locked in the request storage — no refund function exists.

The total fee paid by the user is irrecoverable. This is a direct, permanent loss of user funds with no on-chain recovery path.

---

### Likelihood Explanation

This is analogous to the FBTC "lack of chain whitelisting" finding: it occurs when a user specifies an unsupported/invalid parameter. Realistic triggers include:

- A frontend bug that passes a malformed or stale price feed ID.
- A user manually constructing a transaction with an incorrect `bytes32` price ID.
- A price feed that was valid at request time but whose `updateData` is no longer available from Hermes by the time the provider attempts fulfillment.

The likelihood is **medium** — it does not require a malicious actor; ordinary user or frontend error is sufficient.

---

### Recommendation

Add a whitelist/registry of supported Pyth price feed IDs and validate submitted `priceIds` against it in `requestPriceUpdatesWithCallback` before accepting the fee. Alternatively, implement a request cancellation/refund function that allows users to reclaim their locked fee if a request remains unfulfilled after a timeout period. The latter is also necessary to address the acknowledged TODO about permanently locked funds.

---

### Proof of Concept

1. Deploy `EchoUpgradeable` and register a provider.
2. Call `requestPriceUpdatesWithCallback` with a `priceIds` array containing `bytes32(uint256(0xDEADBEEF))` — a price ID that does not exist in the Pyth oracle.
3. Pay the required fee. The transaction succeeds; `req.fee` is stored and `_state.accruedFeesInWei` is incremented.
4. The provider attempts `executeCallback` with any `updateData`. `pyth.parsePriceFeedUpdates` reverts because no Pyth price update exists for `0xDEADBEEF`.
5. The request remains active indefinitely. The user has no function to call to recover their fee. Funds are permanently locked. [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L128-141)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-160)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
```

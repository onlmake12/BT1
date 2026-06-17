Based on my investigation of the Pyth codebase, I found a valid analog vulnerability in the Echo contract.

---

### Title
Permissionless Provider Registration Combined with Unvalidated `providerToCredit` in `executeCallback` Enables Fee Theft — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `registerProvider` function in `Echo.sol` is fully permissionless. Combined with the `executeCallback` function's caller-controlled `providerToCredit` parameter — which is only constrained during the exclusivity window — an attacker can register as a provider and then redirect fees from legitimate providers' fulfilled requests to their own account.

---

### Finding Description

**Step 1 — Permissionless registration:**

`registerProvider` imposes no access control. Any address can register as a provider:

```solidity
function registerProvider(
    uint96 baseFeeInWei,
    uint96 feePerFeedInWei,
    uint96 feePerGasInWei
) external override {
    ProviderInfo storage provider = _state.providers[msg.sender];
    require(!provider.isRegistered, "Provider already registered");
    provider.baseFeeInWei = baseFeeInWei;
    ...
    provider.isRegistered = true;
``` [1](#0-0) 

**Step 2 — Unvalidated `providerToCredit` after exclusivity period:**

`executeCallback` enforces that only the assigned provider can execute during the exclusivity window. After that window, **any caller can pass any address as `providerToCredit`**, and the full `req.fee` (the fee paid by the original requester) is credited to that arbitrary address:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
...
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [2](#0-1) 

There is **no check** that `providerToCredit == msg.sender` or `providerToCredit == req.provider` outside the exclusivity window. The `req.fee` was set at request time as `msg.value - _state.pythFeeInWei`:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [3](#0-2) 

---

### Impact Explanation

An attacker who registers as a provider (zero cost, permissionless) can:

1. Monitor on-chain requests made to the legitimate default provider.
2. Wait for the `exclusivityPeriodSeconds` to elapse.
3. Fetch valid Pyth price update data from the public Hermes API (this is freely available).
4. Call `executeCallback` with their own registered address as `providerToCredit`, sending enough `msg.value` to cover `pythFee`.
5. Receive `req.fee + msg.value - pythFee` credited to their `accruedFeesInWei`.
6. Withdraw the accrued fees.

The legitimate provider — who set up the infrastructure, committed to serving the request, and was assigned the job — receives nothing. The attacker profits by the full provider fee component of every request they front-run after the exclusivity window.

---

### Likelihood Explanation

- Pyth price update data is publicly available via the Hermes REST API, so the attacker has no barrier to providing valid `updateData`.
- The exclusivity period is a configurable parameter; if it is short (or zero), the attack window opens immediately.
- The attack is purely on-chain, requires no privileged access, and is repeatable for every outstanding request.
- Gas cost to execute is low relative to the fees that can be stolen on high-volume chains.

---

### Recommendation

Replace the caller-controlled `providerToCredit` parameter with `msg.sender` inside `executeCallback`. The executor of the callback should always be the one credited:

```solidity
// Instead of: address providerToCredit (parameter)
// Use:        msg.sender
_state.providers[msg.sender].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

Alternatively, if the design intent is to allow a separate beneficiary, require that `providerToCredit` is explicitly authorized by `msg.sender` or is the registered provider for the request.

---

### Proof of Concept

1. Attacker deploys EOA `attacker` and calls `echo.registerProvider(0, 0, 0)` — no cost, no permission required.
2. Legitimate user calls `echo.requestPriceUpdatesWithCallback{value: 1 ether}(defaultProvider, publishTime, priceIds, gasLimit)`. The contract stores `req.fee = 1 ether - pythFeeInWei` and `req.provider = defaultProvider`.
3. Attacker waits `exclusivityPeriodSeconds` seconds.
4. Attacker fetches valid `updateData` for `priceIds` at `publishTime` from Hermes (`GET /v2/updates/price/{publishTime}`).
5. Attacker calls `echo.executeCallback{value: pythFee}(attacker, sequenceNumber, updateData, priceIds)`.
6. Contract credits `_state.providers[attacker].accruedFeesInWei += req.fee + pythFee - pythFee = req.fee`.
7. Attacker calls `echo.withdrawAsFeeManager(attacker, req.fee)` (having previously set themselves as their own fee manager) and receives `~1 ether`.

The legitimate `defaultProvider` receives zero fees for the request they were assigned. [4](#0-3) [5](#0-4) [1](#0-0)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-165)
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
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);

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

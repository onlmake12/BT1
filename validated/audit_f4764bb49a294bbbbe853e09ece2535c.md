### Title
Missing Fee Sufficiency Check in `executeCallback` Can Permanently Lock User Funds - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
`Echo.executeCallback` does not validate that `req.fee + msg.value >= pythFee` before forwarding the Pyth contract fee. If the Pyth fee increases after a request is made, the callback permanently reverts and the requester's ETH is locked with no cancellation path.

### Finding Description
In `Echo.requestPriceUpdatesWithCallback`, the user pays a fee that is split into the Echo protocol fee and the provider fee:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
_state.accruedFeesInWei += _state.pythFeeInWei;
```

`req.fee` stores the provider's portion (total paid minus the Echo protocol fee). The Pyth contract fee (`pyth.getUpdateFee(updateData)`) is **not** deducted here — it is expected to be covered by the provider's portion at callback time.

In `executeCallback`, the Pyth fee is computed dynamically and forwarded:

```solidity
uint256 pythFee = pyth.getUpdateFee(updateData);
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
    value: pythFee
}(updateData, priceIds, ...);

_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

There is **no guard** that `req.fee + msg.value >= pythFee`. If `pythFee` exceeds `req.fee + msg.value` (e.g., because the Pyth governance raised the single-update fee after the request was submitted), the subtraction underflows and the entire transaction reverts under Solidity 0.8+ checked arithmetic. The request slot is never cleared, and the user's ETH remains locked in the contract.

Critically, the Echo contract has **no cancellation function** — the code itself acknowledges this gap:

```
// TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
// If executeCallback can revert, then funds can be permanently locked in the contract.
```

The `getFee` view function used by callers to size their payment includes `_state.pythFeeInWei` (the Echo protocol fee) but **not** the Pyth contract fee (`pyth.getUpdateFee`). The comment in `getFee` delegates responsibility to the provider:

```
// Note: The provider needs to set its fees to include the fee charged by the Pyth contract.
```

This means the fee quoted to users at request time does not account for a future Pyth fee increase, and there is no on-chain enforcement that the stored `req.fee` will be sufficient at callback time.

### Impact Explanation
If the Pyth contract's `singleUpdateFeeInWei` is raised via governance after a batch of Echo requests are submitted:

1. All in-flight requests whose `req.fee` was sized against the old Pyth fee will fail every `executeCallback` attempt with an arithmetic underflow revert.
2. Because there is no cancellation or refund path, the ETH paid by requesters is permanently locked in the Echo contract.
3. The provider cannot unilaterally resolve the situation without subsidising the fee increase out of pocket for every affected request.

This is the direct analog of the original report's second scenario: "if `msg.value < _maxSubmissionCost + (_maxGas * _gasPriceBid)` the ticket would require manual execution" — except that in Echo there is no manual-execution escape hatch, making the outcome worse.

### Likelihood Explanation
The Pyth contract fee is governed on-chain and has been changed in the past. Any governance action that raises `singleUpdateFeeInWei` between a request's submission and its callback execution triggers this condition. The window can be hours to days depending on how quickly providers fulfill requests. The probability is low but non-zero, and the impact when it occurs is high (permanent fund lock with no recovery path).

### Recommendation
Add an explicit sufficiency check at the top of `executeCallback`:

```solidity
uint256 pythFee = pyth.getUpdateFee(updateData);
if (req.fee + msg.value < pythFee) revert InsufficientFee();
```

Additionally, implement a cancellation / refund function so that requesters can recover their ETH if a request cannot be fulfilled (e.g., after a timeout or a fee increase). This also resolves the broader TODO noted in the contract.

### Proof of Concept
1. Pyth governance raises `singleUpdateFeeInWei` from 1 wei to 100 wei.
2. An existing Echo request was submitted when the fee was 1 wei; `req.fee` was sized accordingly.
3. Provider calls `executeCallback` with `msg.value = 0`.
4. `pythFee = pyth.getUpdateFee(updateData)` returns 100 wei (new fee × number of updates).
5. `parsePriceFeedUpdates{value: 100}(...)` attempts to forward 100 wei; if the contract balance covers it, the call succeeds.
6. `(req.fee + 0) - 100` underflows → revert.
7. The request is never cleared; the user's ETH is permanently locked.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L75-84)
```text
        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L144-162)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L235-255)
```text
    function getFee(
        address provider,
        uint32 callbackGasLimit,
        bytes32[] calldata priceIds
    ) public view override returns (uint96 feeAmount) {
        uint96 baseFee = _state.pythFeeInWei; // Fixed fee to Pyth
        // Note: The provider needs to set its fees to include the fee charged by the Pyth contract.
        // Ideally, we would be able to automatically compute the pyth fees from the priceIds, but the
        // fee computation on IPyth assumes it has the full updated data.
        uint96 providerBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 providerFeedFee = SafeCast.toUint96(
            priceIds.length * _state.providers[provider].feePerFeedInWei
        );
        uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei; // Provider's per-gas rate
        uint256 gasFee = callbackGasLimit * providerFeeInWei; // Total provider fee based on gas
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
    }
```

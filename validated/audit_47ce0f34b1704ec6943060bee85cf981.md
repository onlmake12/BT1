### Title
`executeCallback` Unnecessarily Marked `payable` Causes Keeper ETH Loss — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.executeCallback()` is declared `payable` even though the keeper/fulfiller calling it is not required to supply any ETH. Any ETH accidentally attached to the call is silently credited to the provider's accrued-fee balance and is unrecoverable by the caller. The contract's own TODO comment at line 104 explicitly flags this uncertainty.

---

### Finding Description

`executeCallback` in `Echo.sol` is the function keepers call to fulfill a price-update callback request. Its full fee lifecycle is:

1. **At request time** — the user calls `requestPriceUpdatesWithCallback{value: requiredFee}(...)`. The contract stores `req.fee = msg.value - _state.pythFeeInWei` and increments `accruedFeesInWei` by `pythFeeInWei`. All required ETH is already held by the contract.

2. **At fulfillment time** — the keeper calls `executeCallback(...)`. The function computes `pythFee = pyth.getUpdateFee(updateData)` and pays it to the Pyth oracle from the contract's existing balance (`{value: pythFee}`). No ETH from the keeper is needed.

3. **Fee accounting** — the provider is credited:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

Because `msg.value` is folded into the provider's accrued fees, any ETH the keeper accidentally attaches is permanently transferred to the provider — it is never refunded to the caller.

The developer's own comment directly above the function signature reads:

> `// TODO: does this need to be payable? Any cost paid to Pyth could be taken out of the provider's accrued fees.`

This confirms the `payable` modifier is unintentional and unresolved. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

Any keeper or relayer that accidentally attaches ETH to an `executeCallback` call loses that ETH permanently. The ETH is not trapped in the contract — it is credited to `providerToCredit.accruedFeesInWei` and becomes withdrawable by the provider. The caller has no recourse. This is a direct, irreversible fund loss for the caller. [4](#0-3) 

---

### Likelihood Explanation

`executeCallback` is the primary function called by off-chain keeper bots. Keeper infrastructure commonly uses generic transaction-building libraries that may forward a non-zero `msg.value` (e.g., when reusing a call template from `requestPriceUpdatesWithCallback`, which does require ETH). The function signature gives no indication that ETH is not needed, and the interface declaration in `IEcho.sol` also marks it `payable` without any NatSpec warning. [5](#0-4) 

---

### Recommendation

Remove the `payable` modifier from `executeCallback` in both `Echo.sol` and `IEcho.sol`. The Pyth oracle fee is already funded by the user's deposit stored in `req.fee`; no ETH from the keeper is required or expected.

```solidity
// Before
function executeCallback(
    address providerToCredit,
    uint64 sequenceNumber,
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external payable override {

// After
function executeCallback(
    address providerToCredit,
    uint64 sequenceNumber,
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external override {
```

Also remove `msg.value` from the fee accounting line, since it will always be zero after the fix:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128(req.fee - pythFee);
``` [1](#0-0) 

---

### Proof of Concept

```solidity
// Keeper bot accidentally sends 0.01 ETH with the fulfillment call
echo.executeCallback{value: 0.01 ether}(
    providerAddress,
    sequenceNumber,
    updateData,
    priceIds
);
// The call succeeds. The 0.01 ETH is credited to providerAddress.accruedFeesInWei.
// The keeper's 0.01 ETH is gone with no revert and no refund.
```

The root cause is confirmed at: [4](#0-3)

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

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L70-75)
```text
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable;
```

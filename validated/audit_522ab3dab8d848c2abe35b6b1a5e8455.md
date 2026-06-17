### Title
Unchecked Return Value of ETH Refund `.call()` Permanently Locks Excess Funds — (`File: target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

In `PythAggregatorV3.sol`, the `updateFeeds` function attempts to refund excess ETH to the caller after paying the Pyth update fee. The return value of the low-level `.call()` used for the refund is captured into a `success` variable but is **never validated**. A suppression comment (`// solhint-disable-next-line no-unused-vars`) confirms the developers were aware `success` was unused. If the refund call fails, the transaction completes silently and the excess ETH is permanently locked in the contract with no recovery path.

---

### Finding Description

In `updateFeeds`:

```solidity
function updateFeeds(bytes[] calldata priceUpdateData) public payable {
    uint fee = pyth.getUpdateFee(priceUpdateData);
    pyth.updatePriceFeeds{value: fee}(priceUpdateData);

    // refund remaining eth
    // solhint-disable-next-line no-unused-vars
    (bool success, ) = payable(msg.sender).call{
        value: address(this).balance
    }("");
}
``` [1](#0-0) 

The `success` boolean returned by the `.call()` is never checked with a `require`. If the refund fails — for example, because `msg.sender` is a contract with no `receive()` or `fallback()` function, or one that reverts on ETH receipt — the function returns without reverting, and the excess ETH remains locked in the `PythAggregatorV3` contract. There is no `withdraw` or sweep function in the contract to recover stranded ETH. [2](#0-1) 

---

### Impact Explanation

Any caller that overpays `updateFeeds` and whose address cannot receive ETH (e.g., a contract without a `receive` function, or one that reverts on ETH transfer) will permanently lose the excess ETH. The ETH is locked in the `PythAggregatorV3` contract with no recovery mechanism. This results in a direct, irreversible loss of user funds.

---

### Likelihood Explanation

`PythAggregatorV3` is a Chainlink-compatible wrapper intended to be deployed and called by other smart contracts (DeFi protocols, aggregators). Contract callers are common and many do not implement `receive()`. Any such contract that sends excess ETH to `updateFeeds` — a natural pattern when the exact fee is not known ahead of time — will silently lose funds. The `// solhint-disable-next-line no-unused-vars` comment confirms the unchecked `success` is a known but unresolved issue.

---

### Recommendation

Validate the return value of the refund call:

```solidity
(bool success, ) = payable(msg.sender).call{
    value: address(this).balance
}("");
require(success, "ETH refund failed");
``` [3](#0-2) 

---

### Proof of Concept

1. Deploy a contract `Caller` that has **no** `receive()` or `fallback()` function.
2. `Caller` calls `PythAggregatorV3.updateFeeds{value: 1 ether}(priceUpdateData)` where the actual fee is `1 wei`.
3. `updatePriceFeeds` succeeds, consuming `1 wei`.
4. The contract attempts to refund `1 ether - 1 wei` to `Caller` via `.call()`.
5. The call fails silently because `Caller` cannot receive ETH.
6. `updateFeeds` returns without reverting.
7. `1 ether - 1 wei` is permanently locked in `PythAggregatorV3` with no recovery path.

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L16-38)
```text
contract PythAggregatorV3 {
    bytes32 public priceId;
    IPyth public pyth;

    constructor(address _pyth, bytes32 _priceId) {
        priceId = _priceId;
        pyth = IPyth(_pyth);
    }

    // Wrapper function to update the underlying Pyth price feeds. Not part of the AggregatorV3 interface but useful.
    function updateFeeds(bytes[] calldata priceUpdateData) public payable {
        // Update the prices to the latest available values and pay the required fee for it. The `priceUpdateData` data
        // should be retrieved from our off-chain Price Service API using the `hermes-client` package.
        // See section "How Pyth Works on EVM Chains" below for more information.
        uint fee = pyth.getUpdateFee(priceUpdateData);
        pyth.updatePriceFeeds{value: fee}(priceUpdateData);

        // refund remaining eth
        // solhint-disable-next-line no-unused-vars
        (bool success, ) = payable(msg.sender).call{
            value: address(this).balance
        }("");
    }
```

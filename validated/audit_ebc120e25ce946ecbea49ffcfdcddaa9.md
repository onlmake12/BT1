### Title
Unchecked ETH Refund Return Value Permanently Locks Excess ETH — (`target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3.updateFeeds()` is a `payable` function that pays the exact Pyth fee and then attempts to refund the remaining ETH (`address(this).balance`) to `msg.sender` via a low-level `.call`. The return value of that refund call is silently discarded. If the refund fails — because `msg.sender` is a contract that cannot receive ETH — the excess ETH is permanently locked in the `PythAggregatorV3` contract, which has no `receive()`, no `fallback()`, no admin, and no withdrawal function.

---

### Finding Description

`PythAggregatorV3.updateFeeds()` is the only payable entry point in the contract:

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

The `success` boolean is captured but never checked. If `msg.sender` is a contract that reverts on ETH receipt (e.g., has no `receive()` or `fallback()`, or explicitly reverts), the `.call` returns `false` and the ETH remains in the `PythAggregatorV3` contract. The contract defines no `receive()`, no `fallback()`, no owner, and no sweep/withdrawal function, so the ETH is irrecoverable. [2](#0-1) 

---

### Impact Explanation

Any excess ETH sent to `updateFeeds` by a contract caller that cannot receive ETH is permanently locked in the deployed `PythAggregatorV3` instance. There is no admin, no `selfdestruct`, and no sweep function. The ETH cannot be recovered by any party.

This is a direct analog to the reported vulnerability: in the original report, ETH gets stuck because the `receive()` guard is insufficient; here, ETH gets stuck because the refund's failure is silently ignored and there is no recovery path.

---

### Likelihood Explanation

`PythAggregatorV3` is an official Pyth SDK contract recommended for Chainlink-compatible integrations. It is commonly deployed by DeFi protocols that call `updateFeeds` from within their own smart contracts. Any such caller contract that lacks a `receive()` function — which is common for contracts that do not expect to receive ETH — will silently lose excess ETH on every `updateFeeds` call where `msg.value > fee`. The scenario is realistic and reachable by any unprivileged user or integrating contract.

---

### Recommendation

Check the return value of the refund call and revert if it fails:

```solidity
(bool success, ) = payable(msg.sender).call{value: address(this).balance}("");
require(success, "ETH refund failed");
```

Alternatively, only refund if there is a non-zero excess:

```solidity
uint256 excess = address(this).balance;
if (excess > 0) {
    (bool success, ) = payable(msg.sender).call{value: excess}("");
    require(success, "ETH refund failed");
}
```

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to a live Pyth contract.
2. Deploy an attacker contract with **no** `receive()` or `fallback()` function.
3. From the attacker contract, call `updateFeeds{value: fee + 1 ether}(data)`.
4. The Pyth fee is paid correctly; the refund of `1 ether` silently fails (`success == false`).
5. `address(pythAggregatorV3).balance == 1 ether` — permanently locked with no recovery path. [3](#0-2)

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

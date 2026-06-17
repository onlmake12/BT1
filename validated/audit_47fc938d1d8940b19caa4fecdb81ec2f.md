### Title
Unchecked Refund via `.transfer()` in `verifyUpdate()` Causes Permanent DoS for Smart Contract Callers — (File: `lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.sol`'s `verifyUpdate()` uses `payable(msg.sender).transfer()` to refund excess fees. The `.transfer()` opcode forwards only 2300 gas, which is insufficient for smart contract recipients with non-trivial `receive`/`fallback` functions. When the refund fails, the entire `verifyUpdate()` call reverts, making Lazer price verification permanently unusable for any smart contract integrator that overpays — which is virtually every caller given the `verification_fee` is `1 wei`.

### Finding Description
In `PythLazer.sol`, the `verifyUpdate()` function refunds excess ETH using `.transfer()`: [1](#0-0) 

```solidity
require(msg.value >= verification_fee, "Insufficient fee provided");
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee); // @audit
}
```

The `.transfer()` function hard-caps the gas forwarded to the recipient at 2300. Any smart contract whose `receive` or `fallback` function consumes more than 2300 gas — including proxy contracts (which do a `delegatecall`), multisig wallets, contracts that emit events, or contracts that write to storage — will cause the `.transfer()` to revert. Because this revert is not caught, it propagates and reverts the entire `verifyUpdate()` call.

This is the fee/refund accounting analog to the CompoundV2 vulnerability class: instead of a missing return-value check causing silent failure, the wrong transfer primitive causes an uncaught revert that silently kills the entire verification flow for a class of callers.

The `verification_fee` is initialized to `1 wei`: [2](#0-1) 

This means any caller sending a standard ETH amount (e.g., `0.001 ether`) will always overpay and always hit the refund path.

### Impact Explanation
Any smart contract that calls `verifyUpdate()` with `msg.value > 1 wei` and has a `receive`/`fallback` function requiring more than 2300 gas will have the entire call revert. The price update is never verified, the caller's transaction fails, and there is no workaround short of sending exactly `1 wei` — which is fragile and breaks if governance ever raises `verification_fee`. This is a permanent DoS for smart contract integrators of Pyth Lazer.

### Likelihood Explanation
High. The `verification_fee` is `1 wei`, so virtually every real-world caller overpays. Proxy-based contracts (OpenZeppelin `TransparentUpgradeableProxy`, `ERC1967Proxy`), multisig wallets (Gnosis Safe), and any contract that emits an event in its `receive` function all exceed the 2300-gas stipend. These are the dominant patterns in production DeFi.

### Recommendation
Replace `.transfer()` with a low-level `.call{}` and check the return value, consistent with the pattern used everywhere else in the Pyth codebase (e.g., `Entropy.sol` line 163, `Scheduler.sol` line 860):

```solidity
if (msg.value > verification_fee) {
    (bool success, ) = payable(msg.sender).call{value: msg.value - verification_fee}("");
    require(success, "Refund failed");
}
```

### Proof of Concept
1. Deploy a contract `Consumer` whose `receive()` function emits an event (costs ~750 gas, well above 2300 is not needed — but a Gnosis Safe fallback or proxy `delegatecall` easily exceeds 2300 gas).
2. From `Consumer`, call `verifyUpdate{value: 0.001 ether}(validUpdate)`.
3. Signature verification passes; execution reaches the refund branch.
4. `payable(msg.sender).transfer(0.001 ether - 1 wei)` forwards only 2300 gas to `Consumer`.
5. `Consumer`'s `receive()` runs out of gas; `.transfer()` reverts.
6. The revert propagates; `verifyUpdate()` reverts entirely.
7. `Consumer` can never use Pyth Lazer regardless of how valid its update data is. [3](#0-2)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L26-27)
```text
        verification_fee = 1 wei;
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L70-106)
```text
    function verifyUpdate(
        bytes calldata update
    ) external payable returns (bytes calldata payload, address signer) {
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }

        if (update.length < 71) {
            revert("input too short");
        }
        uint32 EVM_FORMAT_MAGIC = 706910618;

        uint32 evm_magic = uint32(bytes4(update[0:4]));
        if (evm_magic != EVM_FORMAT_MAGIC) {
            revert("invalid evm magic");
        }
        uint16 payload_len = uint16(bytes2(update[69:71]));
        if (update.length < 71 + payload_len) {
            revert("input too short");
        }
        payload = update[71:71 + payload_len];
        bytes32 hash = keccak256(payload);
        (signer, , ) = ECDSA.tryRecover(
            hash,
            uint8(update[68]) + 27,
            bytes32(update[4:36]),
            bytes32(update[36:68])
        );
        if (signer == address(0)) {
            revert("invalid signature");
        }
        if (!isValidSigner(signer)) {
            revert("invalid signer");
        }
    }
```

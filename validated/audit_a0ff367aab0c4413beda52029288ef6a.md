### Title
Unsafe `address.transfer()` for ETH Refund Blocks Contract Callers in `verifyUpdate` - (File: `lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.verifyUpdate()` uses the deprecated `address.transfer()` to refund excess ETH to callers. This is the ETH-native analog of the reported ERC20 SafeERC20 issue: `address.transfer()` forwards only 2300 gas, causing the entire call to revert when `msg.sender` is a contract whose `receive`/`fallback` function consumes more than 2300 gas. Any contract integration that overpays is permanently blocked from using `verifyUpdate`.

### Finding Description
In `PythLazer.sol`, the `verifyUpdate` function accepts a fee and refunds any excess ETH to the caller:

```solidity
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
```

`address.transfer()` is a Solidity built-in that forwards exactly 2300 gas to the recipient. This is insufficient for any contract recipient whose `receive` or `fallback` function performs non-trivial work — including multisig wallets (e.g., Gnosis Safe), proxy contracts, or any contract that emits events or writes storage on receipt. When the refund fails, the entire `verifyUpdate` call reverts, including the signature verification work already done.

The rest of the Pyth EVM codebase has already adopted the safe pattern: `Entropy.sol`, `Echo.sol`, `Scheduler.sol`, and `PythGovernance.sol` all use `address.call{value: ...}("")` with a success check. `PythLazer.sol` is the sole outlier. [1](#0-0) 

### Impact Explanation
Any contract that calls `verifyUpdate` and sends `msg.value > verification_fee` will have its transaction revert if its `receive`/`fallback` uses more than 2300 gas. This is a denial-of-service against contract-based Lazer integrations (DeFi protocols, multisig-operated bots, proxy-based callers) that overpay. The `verifyUpdate` function is the sole entry point for Lazer price verification; blocking it prevents the integration from consuming Lazer data entirely until the caller is rewritten to send exactly `verification_fee`.

### Likelihood Explanation
Moderate. Contract integrations commonly send a small buffer above the required fee to guard against fee increases between estimation and execution. Any such contract whose fallback is non-trivial (multisig, proxy, event-emitting receiver) will be affected. The `verification_fee` is currently 1 wei, making exact-amount calls easy today, but the fee is owner-adjustable; as the fee grows, overpayment becomes more common. [2](#0-1) [3](#0-2) 

### Recommendation
Replace `address.transfer()` with a low-level `call`, consistent with every other ETH-sending function in the codebase:

```solidity
if (msg.value > verification_fee) {
    (bool sent, ) = payable(msg.sender).call{value: msg.value - verification_fee}("");
    require(sent, "Refund failed");
}
```

Reference: `Entropy.sol` line 163, `Scheduler.sol` line 660, `PythGovernance.sol` line 268 all use this pattern. [4](#0-3) [5](#0-4) 

### Proof of Concept
1. Deploy a contract `Attacker` with a `receive()` function that emits an event (costs >2300 gas).
2. From `Attacker`, call `pythLazer.verifyUpdate{value: 2 wei}(validUpdate)` where `verification_fee == 1 wei`.
3. The signature verification succeeds, but `payable(msg.sender).transfer(1 wei)` reverts because `Attacker.receive()` exceeds the 2300 gas stipend.
4. The entire transaction reverts; `Attacker` cannot use `verifyUpdate` regardless of how many times it retries with `msg.value > verification_fee`. [6](#0-5)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L10-10)
```text
    uint256 public verification_fee;
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L26-26)
```text
        verification_fee = 1 wei;
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L163-164)
```text
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L660-661)
```text
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send funds");
```

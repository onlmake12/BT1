### Title
Lazer EVM `verifyUpdate()` Signed Payload Lacks Chain ID and Contract Address Binding, Enabling Cross-Chain Replay — (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate()` computes the signature hash as `keccak256(payload)` over the raw Lazer price payload. Neither `address(this)` nor `block.chainid` is included in the signed data. Because Pyth Lazer is deployed on multiple EVM chains with the same set of trusted signers, a valid signed update captured from one chain passes signature verification on any other chain.

---

### Finding Description

In `lazer/contracts/evm/src/PythLazer.sol`, the `verifyUpdate()` function extracts the payload and hashes it directly:

```solidity
payload = update[71:71 + payload_len];
bytes32 hash = keccak256(payload);
(signer, , ) = ECDSA.tryRecover(
    hash,
    uint8(update[68]) + 27,
    bytes32(update[4:36]),
    bytes32(update[36:68])
);
``` [1](#0-0) 

The `payload` itself contains only a format magic, a timestamp, a channel byte, and feed data:

```
PAYLOAD_FORMAT_MAGIC (4 bytes) | timestamp (8 bytes) | channel (1 byte) | feeds...
``` [2](#0-1) 

Neither `address(this)` (the `PythLazer` proxy address) nor `block.chainid` is mixed into the hash before signing. The trusted-signer registry is shared across all EVM deployments of `PythLazer` (Ethereum, Arbitrum, Base, etc.), so the same ECDSA key is accepted on every chain. [3](#0-2) 

---

### Impact Explanation

An unprivileged relayer or Lazer updater who observes a valid signed update on Chain A can submit that exact byte-string to `verifyUpdate()` on Chain B. The call will succeed: the signature is valid, the signer is trusted, and the returned `payload` is accepted as authentic by any downstream consumer contract.

Concrete harm scenarios:

1. **Stale-price injection across chains.** If the oracle has already published a newer update on Chain B (price moved), an attacker can front-run or delay the legitimate relay and instead submit the older update captured from Chain A. Consumer contracts that do not enforce a strict freshness window will consume the stale price.

2. **Same-chain multi-contract replay.** If multiple `PythLazer` proxy instances exist on the same chain (e.g., a staging deployment and a production deployment sharing a trusted signer), a signature produced for one contract is unconditionally valid on the other.

3. **Future divergence risk.** If the Lazer oracle ever begins signing chain-specific payloads (e.g., different fee tiers, chain-specific feed IDs), the absence of domain separation means those payloads remain cross-replayable.

---

### Likelihood Explanation

- Pyth Lazer is already deployed on multiple EVM chains with the same trusted-signer set.
- Any party subscribed to the Lazer WebSocket stream receives the signed `evm` binary blob and can submit it to any chain.
- No privileged access is required; `verifyUpdate()` is a public payable function callable by anyone.
- The attack requires only capturing a broadcast update and submitting it to a second chain — a one-transaction operation. [4](#0-3) 

---

### Recommendation

Bind the signed payload to the specific contract and chain by including a domain separator in the hash before the oracle signs it, analogous to EIP-712:

```solidity
bytes32 hash = keccak256(abi.encodePacked(
    block.chainid,
    address(this),
    payload
));
```

This requires a coordinated change in the off-chain Lazer signing service to include `chainId` and `contractAddress` in the pre-image. Alternatively, adopt a full EIP-712 domain separator so that signatures are scoped to a specific `(chainId, verifyingContract)` pair.

---

### Proof of Concept

1. Subscribe to the Lazer WebSocket on Ethereum mainnet and capture a valid `evm`-format update blob `U` (a hex string beginning with magic `0x2a22999a...`).
2. On Arbitrum (or any other EVM chain where `PythLazer` is deployed with the same trusted signer), call:
   ```solidity
   pythLazer.verifyUpdate{value: fee}(U);
   ```
3. The call succeeds and returns `(payload, signer)` — the Arbitrum contract accepts the Ethereum-originated signature as valid, with no chain binding enforced. [4](#0-3) [5](#0-4)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-106)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
    }

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

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L122-136)
```text
        uint32 FORMAT_MAGIC = 2479346549;

        pos = 0;
        uint32 magic = _readBytes4(update, pos);
        pos += 4;
        if (magic != FORMAT_MAGIC) {
            revert("invalid magic");
        }
        timestamp = _readBytes8(update, pos);
        pos += 8;
        channel = PythLazerStructs.Channel(_readBytes1(update, pos));
        pos += 1;
        feedsLen = uint8(_readBytes1(update, pos));
        pos += 1;
    }
```

**File:** lazer/contracts/evm/test/PythLazer.t.sol (L45-75)
```text
    function test_verify() public {
        // Prepare dummy update and signer
        address trustedSigner = 0xb8d50f0bAE75BF6E03c104903d7C3aFc4a6596Da;
        vm.prank(owner);
        pythLazer.updateTrustedSigner(trustedSigner, 3000000000000000);
        bytes
            memory update = hex"2a22999a9ee4e2a3df5affd0ad8c7c46c96d3b5ef197dd653bedd8f44a4b6b69b767fbc66341e80b80acb09ead98c60d169b9a99657ebada101f447378f227bffbc69d3d01003493c7d37500062cf28659c1e801010000000605000000000005f5e10002000000000000000001000000000000000003000104fff8";

        uint256 fee = pythLazer.verification_fee();

        address alice = makeAddr("alice");
        vm.deal(alice, 1 ether);
        address bob = makeAddr("bob");
        vm.deal(bob, 1 ether);

        // Alice provides appropriate fee
        vm.prank(alice);
        pythLazer.verifyUpdate{value: fee}(update);
        assertEq(alice.balance, 1 ether - fee);

        // Alice overpays and is refunded
        vm.prank(alice);
        pythLazer.verifyUpdate{value: 0.5 ether}(update);
        assertEq(alice.balance, 1 ether - fee - fee);

        // Bob does not attach a fee
        vm.prank(bob);
        vm.expectRevert("Insufficient fee provided");
        pythLazer.verifyUpdate(update);
        assertEq(bob.balance, 1 ether);
    }
```

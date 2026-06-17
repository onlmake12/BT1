### Title
Missing Domain Separation in `verifyUpdate` Enables Cross-Chain Replay of Lazer Price Updates - (File: `lazer/contracts/evm/src/PythLazer.sol`)

### Summary

The `verifyUpdate` function in `PythLazer.sol` computes the signature hash as `keccak256(payload)` with no chain ID, contract address, or any chain-specific context included in the signed data. This is the on-chain analog of the external report's "event-based" authentication: the cryptographic check is not bound to the specific execution context. Any valid signed Lazer update accepted on one EVM chain is also accepted on every other EVM chain where the same trusted signer is registered, enabling an unprivileged relayer to replay cross-chain price updates and inject stale or mismatched prices into consumer contracts.

### Finding Description

In `PythLazer.sol`, `verifyUpdate` extracts the payload and computes the verification hash as follows:

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

The signed message is purely `keccak256(payload)`. The payload structure, parsed by `PythLazerLib.parsePayloadHeader`, contains: a 4-byte magic, an 8-byte timestamp, a 1-byte channel, a 1-byte feed count, and then feed data. [2](#0-1) 

There is no `block.chainid`, no `address(this)`, no nonce, and no sequence number anywhere in the signed data. The Lazer governance mechanism registers the same trusted signer public key across multiple EVM chains via `UpdateTrustedSigner264Bit`/`UpdateTrustedSigner256Bit` governance actions. [3](#0-2) 

The `isValidSigner` check is purely a state lookup against `trustedSignerToExpiresAtMapping[signer]`: [4](#0-3) 

Because the signed bytes carry no chain-binding context, a valid `update` blob accepted on chain A (e.g., Ethereum mainnet) is byte-for-byte identical to a valid `update` blob on chain B (e.g., Arbitrum, Base, BNB Chain) — the signature check passes on both.

**Exploit path:**

1. Attacker monitors the mempool or event logs on chain A and captures a valid `update` blob submitted to `verifyUpdate`.
2. Attacker submits the identical `update` bytes to `verifyUpdate` on chain B within the consumer's freshness window.
3. `verifyUpdate` on chain B recovers the same signer, confirms it is in `trustedSignerToExpiresAtMapping`, and returns `(payload, signer)` — accepting the cross-chain replay.
4. The consumer contract on chain B processes the payload as if it were a legitimate chain-B update.

The attack requires no privileged access, no key material, and no governance interaction. Any party who can observe on-chain transactions and submit transactions to another chain can execute it.

### Impact Explanation

Consumer contracts on chain B receive a payload that was signed for chain A. Concretely:

- **Stale price injection**: If the Lazer distributor has not yet pushed an update to chain B, an attacker replays the most recent chain-A update. Consumer contracts accept it as fresh (timestamp is within the freshness window) while the chain-B distributor's update is suppressed.
- **Cross-channel price injection**: If the Lazer distributor uses different channels (e.g., `RealTime` vs `FixedRate`) per chain, an attacker replays a payload from the wrong channel, causing consumer contracts to use prices from an unintended channel.
- **Price manipulation in DeFi**: A lending protocol or DEX on chain B that uses Lazer prices for liquidation or trade execution could be manipulated into using incorrect prices, causing incorrect liquidations or arbitrage losses for users.

The `verifyUpdate` function returns the payload and signer to the caller with no further validation; the consumer contract is the only line of defense, and it has no way to distinguish a legitimate chain-B update from a replayed chain-A update.

### Likelihood Explanation

- The same Lazer trusted signer key is registered on multiple EVM chains by design (governance actions target multiple chains with the same public key).
- Valid `update` blobs are publicly observable on-chain (calldata of any `verifyUpdate` call).
- Submitting a transaction to a second chain costs only gas.
- No privileged access, leaked keys, or social engineering is required.
- The attack window is the consumer's price freshness window (typically seconds to minutes), which is easily achievable given cross-chain transaction latency.

### Recommendation

Include `block.chainid` and `address(this)` in the signed message hash, following EIP-712 domain separation. The Lazer distributor must sign:

```solidity
bytes32 hash = keccak256(abi.encodePacked(block.chainid, address(this), payload));
```

Or adopt a full EIP-712 domain separator:

```solidity
bytes32 DOMAIN_SEPARATOR = keccak256(abi.encode(
    keccak256("EIP712Domain(string name,uint256 chainId,address verifyingContract)"),
    keccak256("PythLazer"),
    block.chainid,
    address(this)
));
bytes32 hash = keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR, keccak256(payload)));
```

The Lazer distributor's signing logic must be updated to include the target chain ID and contract address in the signed message. This is the "result-based" analog: the signature is cryptographically bound to the specific chain and contract, making cross-chain replay impossible without the distributor's private key.

### Proof of Concept

1. Deploy `PythLazer` on a local fork of Ethereum (chainId=1) and Arbitrum (chainId=42161), both with the same trusted signer address registered via `updateTrustedSigner`.
2. Construct a valid Lazer `update` blob signed by the trusted signer (as done in `PythLazer.t.sol` `test_verify`).
3. Call `verifyUpdate{value: fee}(update)` on the Ethereum fork — succeeds, returns `(payload, signer)`.
4. Call `verifyUpdate{value: fee}(update)` on the Arbitrum fork with the **identical bytes** — also succeeds, returns the same `(payload, signer)`.
5. Both chains accept the same signed blob, confirming that the signature provides no chain binding. [5](#0-4)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-68)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L92-99)
```text
        payload = update[71:71 + payload_len];
        bytes32 hash = keccak256(payload);
        (signer, , ) = ECDSA.tryRecover(
            hash,
            uint8(update[68]) + 27,
            bytes32(update[4:36]),
            bytes32(update[36:68])
        );
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L110-136)
```text
    function parsePayloadHeader(
        bytes memory update
    )
        public
        pure
        returns (
            uint64 timestamp,
            PythLazerStructs.Channel channel,
            uint8 feedsLen,
            uint16 pos
        )
    {
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

**File:** contract_manager/scripts/check_proposal.ts (L241-265)
```typescript
      if (
        instruction.governanceAction instanceof UpdateTrustedSigner264Bit ||
        instruction.governanceAction instanceof UpdateTrustedSigner256Bit
      ) {
        const { targetChainId, publicKey, expiresAt } =
          instruction.governanceAction;

        console.log(
          `Verifying ${instruction.governanceAction.action} on '${targetChainId}'`,
        );

        const expiresAtMs = expiresAt * 1000n;
        if (expiresAtMs > Number.MAX_SAFE_INTEGER) {
          console.error(
            "expiration value in milliseconds cannot be represented as a JS integer:",
            expiresAtMs,
          );
          continue;
        }
        const expiresAtDate = new Date(Number(expiresAtMs));

        console.log("Trusted signer proposal info:");
        console.log("  public key:", publicKey);
        console.log("  expires at:", expiresAtDate);
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

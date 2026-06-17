### Title
Cross-Chain Replay of Lazer Price Updates Due to Missing Chain Binding in Signature — (`lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate()` computes the signature hash as `keccak256(payload)` with no chain ID or contract address included. Because the same trusted signer key is registered across every EVM deployment of `PythLazer`, a valid signed update from chain A is cryptographically indistinguishable from a valid update on chain B. An unprivileged attacker can capture a signed update on one chain and replay it on any other chain where the same signer is trusted, causing consuming contracts to accept a cross-chain-replayed (potentially stale) price.

---

### Finding Description

In `PythLazer.sol`, `verifyUpdate()` extracts the payload and verifies the signer:

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

The `payload` is parsed by `PythLazerLib.parsePayloadHeader()` and contains only: a format magic, a timestamp, a channel byte, and feed data.

```solidity
uint32 FORMAT_MAGIC = 2479346549;
// ...
timestamp = _readBytes8(update, pos);
channel = PythLazerStructs.Channel(_readBytes1(update, pos));
feedsLen = uint8(_readBytes1(update, pos));
``` [2](#0-1) 

**Neither `block.chainid` nor `address(this)` is included in the signed data.** The same trusted signer address is registered on every EVM chain via `updateTrustedSigner()`. The `isValidSigner()` check only verifies expiry:

```solidity
function isValidSigner(address signer) public view returns (bool) {
    return block.timestamp < trustedSignerToExpiresAtMapping[signer];
}
``` [3](#0-2) 

This is the direct analog to the Farcaster `_verifyRemoveSig` bug: just as that signature omits `fid` and can therefore be applied to a different `fid`, the Lazer signature omits `chainId`/`contractAddress` and can therefore be applied to a different chain's `PythLazer` contract.

---

### Impact Explanation

An attacker who observes a valid signed Lazer update on chain A can immediately submit it to `verifyUpdate()` on chain B. The function returns `(payload, signer)` as verified. The consuming contract on chain B receives a payload that:

1. Was signed by a trusted signer — passes `isValidSigner`.
2. Contains a timestamp from chain A's update cycle — which may be slightly older than the legitimate chain B update that has not yet arrived.

If the consuming contract does not enforce a tight staleness window (or relies on `verifyUpdate` to do so — which it does not), the attacker can feed a stale price to a DeFi protocol on chain B. This is exploitable in any scenario where the attacker has a leveraged position and can profit from a briefly stale price (e.g., avoiding liquidation, executing a favorable trade).

The `verifyUpdate` function performs **no timestamp validation** — it is entirely left to the caller: [4](#0-3) 

---

### Likelihood Explanation

- `PythLazer` is deployed on multiple EVM chains with the same trusted signer key.
- The EVM format magic (`706910618`) and payload magic (`2479346549`) are identical across all deployments.
- Any unprivileged actor can call `verifyUpdate()` — it is a `payable` function requiring only the `verification_fee`.
- Monitoring multiple chains for signed updates is trivial using public RPC endpoints or the Lazer WebSocket API.
- The attack requires no privileged access, no leaked keys, and no governance majority.

---

### Recommendation

Include `block.chainid` and `address(this)` in the signed digest, analogous to EIP-712 domain separation:

```solidity
bytes32 hash = keccak256(abi.encodePacked(block.chainid, address(this), payload));
```

This ensures a signature produced for chain A is cryptographically invalid on chain B, even if the same signer key is trusted on both.

---

### Proof of Concept

1. Deploy `PythLazer` on Ethereum (chain 1) and Arbitrum (chain 42161) with the same `trustedSigner` address registered via `updateTrustedSigner()`.
2. Subscribe to the Lazer WebSocket and receive a valid signed `update` blob at timestamp `T` on Ethereum.
3. On Arbitrum, call `pythLazer.verifyUpdate{value: fee}(update)` with the Ethereum-origin blob.
4. `verifyUpdate` recovers the signer from `keccak256(payload)`, finds it in `trustedSignerToExpiresAtMapping`, and returns `(payload, signer)` — **no revert**.
5. The consuming contract on Arbitrum processes the payload as a freshly verified Lazer price, unaware it originated from Ethereum's update cycle at time `T` rather than Arbitrum's current time `T+δ`.
6. If the attacker times this replay to precede the legitimate Arbitrum update, the consuming contract holds a stale price for the duration of the delay, which the attacker can exploit in a DeFi protocol. [4](#0-3) [5](#0-4)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-68)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
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

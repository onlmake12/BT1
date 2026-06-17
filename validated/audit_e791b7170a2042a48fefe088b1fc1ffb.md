### Title
Unchecked `Channel` Enum Cast in `parsePayloadHeader` Accepts Out-of-Range Values — (`File: lazer/contracts/evm/src/PythLazerLib.sol`)

### Summary
`PythLazerLib.parsePayloadHeader` casts the raw channel byte from a Lazer payload directly into the `PythLazerStructs.Channel` enum without validating that the value falls within the defined enum range. A Lazer updater/relayer can craft a payload whose channel byte is `0` (`Channel.Invalid`) or any value `>= 5` (undefined), and the function will silently accept it. Consumer contracts that gate logic on the `channel` field will receive a semantically invalid channel value with no revert.

### Finding Description

In `PythLazerLib.parsePayloadHeader`, the channel byte is read and cast directly:

```solidity
channel = PythLazerStructs.Channel(_readBytes1(update, pos));
```

`PythLazerStructs.Channel` is defined as:

```solidity
enum Channel {
    Invalid,   // 0
    RealTime,  // 1
    FixedRate50,  // 2
    FixedRate200, // 3
    FixedRate1000 // 4
}
```

Solidity enums do **not** perform bounds checking on explicit casts from integer types. Any `uint8` value (0–255) can be cast to `Channel` without a revert. The value `0` maps to `Channel.Invalid`, which is explicitly named as invalid but is never rejected. Values `5`–`255` produce out-of-range enum members that have no defined meaning.

By contrast, the Sui implementation (`channel_v2.move`) correctly validates:
```move
public(package) fun from_u8(value: u8): Channel {
    assert!(value >= 1 && value <= 4, EChannelOutOfRange);
    Channel(value)
}
```

And the deprecated Sui `channel.move` also aborts on unknown values. The EVM `PythLazerLib` has no equivalent guard.

The `parseFeedProperty` function in the same file does validate its range:
```solidity
require(propertyId <= 12, "Unknown property");
```
This shows the pattern is known and intentionally applied elsewhere, but was omitted for the `Channel` field.

### Impact Explanation

Consumer contracts that call `parsePayloadHeader` or `parseUpdateFromPayload` and then branch on the returned `channel` value (e.g., to enforce that only `RealTime` or a specific `FixedRate` channel is accepted) will silently receive `Channel.Invalid` (0) or an undefined enum value. If the consumer does not independently validate the channel, it may:

- Accept a price update on a channel it did not intend to accept (e.g., `Channel.Invalid` passes a `channel != Channel.Invalid` check only if the consumer explicitly checks for it).
- Silently store a price update with a semantically meaningless channel tag, corrupting downstream channel-gated logic.

The `parseUpdateFromPayload` function stores the channel directly into the returned `Update` struct with no validation, so any consumer using that higher-level API is equally affected.

### Likelihood Explanation

The Lazer update payload is submitted by any external relayer or user who calls a consumer contract's update function. The payload bytes are attacker-controlled. The only upstream check is the ECDSA signature verification in `PythLazer.verifyUpdate`, which authenticates the payload but does not constrain the channel byte — a trusted Lazer signer could produce a payload with channel `0` or `5+`, or a replay/mutation attack on the channel byte after signature verification is not possible, but a legitimately signed payload with an invalid channel byte is entirely possible if the signer is compromised or if the protocol adds new channel IDs in the future that the EVM contract does not yet recognize.

### Recommendation

Add a bounds check in `parsePayloadHeader` immediately after reading the channel byte, mirroring the pattern already used for `parseFeedProperty`:

```solidity
uint8 channelRaw = _readBytes1(update, pos);
require(channelRaw >= 1 && channelRaw <= 4, "Invalid channel");
channel = PythLazerStructs.Channel(channelRaw);
```

This matches the validation logic in the Sui `channel_v2.move` implementation and the existing `parseFeedProperty` guard in the same file.

### Proof of Concept

1. Craft a valid Lazer payload (passing ECDSA verification) with the channel byte set to `0x00` (i.e., `Channel.Invalid`).
2. Call `PythLazerLib.parsePayloadHeader(payload)`.
3. Observe that the function returns without reverting, with `channel == PythLazerStructs.Channel.Invalid`.
4. A consumer contract that checks `channel == PythLazerStructs.Channel.RealTime` will correctly reject it, but a consumer that does not check (or checks `channel != Channel.Invalid` expecting that to be impossible) will silently accept the update.
5. Repeat with channel byte `0x05` (undefined enum member) — again no revert.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L130-135)
```text
        timestamp = _readBytes8(update, pos);
        pos += 8;
        channel = PythLazerStructs.Channel(_readBytes1(update, pos));
        pos += 1;
        feedsLen = uint8(_readBytes1(update, pos));
        pos += 1;
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L161-163)
```text
        uint8 propertyId = _readBytes1(update, pos);
        require(propertyId <= 12, "Unknown property");
        property = PythLazerStructs.PriceFeedProperty(propertyId);
```

**File:** lazer/contracts/evm/src/PythLazerStructs.sol (L5-11)
```text
    enum Channel {
        Invalid,
        RealTime,
        FixedRate50,
        FixedRate200,
        FixedRate1000
    }
```

**File:** lazer/contracts/sui/sources/channel_v2.move (L9-11)
```text
public(package) fun from_u8(value: u8): Channel {
    assert!(value >= 1 && value <= 4, EChannelOutOfRange);
    Channel(value)
```

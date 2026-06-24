Audit Report

## Title
`ckb init --import-spec -` double-encodes stdin instead of decoding it, crashing on TOML parse — (`ckb-bin/src/subcommand/init.rs`)

## Summary
When `ckb init` is invoked with `--import-spec -`, the code reads base64-encoded spec content from stdin and is clearly intended to decode it before writing the spec file. Instead, line 197 calls `.encode()` on the already-encoded input, writing a double-base64-encoded blob. The subsequent `AppConfig::load_for_subcommand` call at line 219 attempts to parse that blob as TOML, fails, and the process exits with `ExitCode::Failure`.

## Finding Description
At [1](#0-0)  the `base64_config` is constructed with `with_decode_allow_trailing_bits(true)` — a decode-only configuration option — and the input variable is named `encoded_content`, both confirming the intent to decode. However, `base64_engine.encode(encoded_content.trim())` is called at line 197, re-encoding the input. The resulting file contains a base64 string of a base64 string, which is not valid TOML. At [2](#0-1)  `AppConfig::load_for_subcommand` attempts to parse the written file as a chain spec (TOML), fails, and the `?` operator propagates the error, crashing the `init` subcommand unconditionally.

## Impact Explanation
Any user running `ckb init --import-spec -` with a valid base64-encoded spec on stdin will always receive a crash with a non-zero exit code, and the spec file will be left in a corrupt (double-encoded) state. This matches the allowed CKB bounty impact: **Any local command line crash (Note, 0–500 pts)**.

## Likelihood Explanation
The `--import-spec -` stdin path is a documented workflow. Any operator following it hits this bug unconditionally — 100% reproduction rate. No special privileges or unusual conditions are required.

## Recommendation
Replace line 197 with a decode call:
```rust
// wrong
let spec_content = base64_engine.encode(encoded_content.trim());
// correct
let spec_content = base64_engine.decode(encoded_content.trim())
    .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
```

## Proof of Concept
```bash
BASE64_SPEC=$(base64 < specs/dev.toml)
echo "$BASE64_SPEC" | ckb init --import-spec - --chain dev --root-dir /tmp/ckb-test
# Expected: Genesis Hash printed
# Actual:   TOML parse error, non-zero exit; specs/dev.toml contains a base64 blob
```

### Citations

**File:** ckb-bin/src/subcommand/init.rs (L193-198)
```rust
            let base64_config =
                base64::engine::GeneralPurposeConfig::new().with_decode_allow_trailing_bits(true);
            let base64_engine =
                base64::engine::GeneralPurpose::new(&base64::alphabet::STANDARD, base64_config);
            let spec_content = base64_engine.encode(encoded_content.trim());
            fs::write(target_file, spec_content)?;
```

**File:** ckb-bin/src/subcommand/init.rs (L219-228)
```rust
    let genesis_hash = AppConfig::load_for_subcommand(args.root_dir, cli::CMD_INIT)?
        .chain_spec()?
        .build_genesis()
        .map_err(|err| {
            eprintln!(
                "Couldn't build the genesis block from the generated chain spec, since {err}"
            );
            ExitCode::Failure
        })?
        .hash();
```

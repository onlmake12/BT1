[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** script/src/error.rs (L49-51)
```rust
    /// Interrupts, such as a Ctrl-C signal
    #[error("VM Interrupts")]
    Interrupts,
```

**File:** script/src/error.rs (L195-202)
```rust
impl From<TransactionScriptError> for Error {
    fn from(error: TransactionScriptError) -> Self {
        match error.cause {
            ScriptError::Interrupts => ErrorKind::Internal
                .because(InternalErrorKind::Interrupts.other(ScriptError::Interrupts.to_string())),
            _ => ErrorKind::Script.because(error),
        }
    }
```

**File:** error/src/internal.rs (L53-54)
```rust
    /// Interrupts, such as a Ctrl-C signal
    Interrupts,
```

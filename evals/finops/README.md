# FinOps evaluation

This offline suite uses only deterministic synthetic fixtures. It gates:

- the exact UsageDetails continuation chain, duplicate-aware cost reconciliation, and zero
  managed-scope leakage;
- deterministic escaped report HTML with CSP nonce protection and no view-time tool calls;
- a median-of-three comparison proving at least a 5x speedup on 20,000 rows and 30 scopes;
- at least a 35% scheduled-task prompt byte reduction from the recorded 52,449-byte baseline;
- the existing pytest suite plus focused runner tests when `--with-tests` is supplied.

Run the complete evaluation:

```bash
python3 evals/finops/run.py --with-tests
```

Use `--output PATH` to also write the JSON result. The process exits nonzero if any gate fails.
The prompt-size gate is expected to remain green for canonically rendered scheduled-task YAML.

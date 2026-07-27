# Ports

Product code depends on the typed contracts in this package rather than concrete
model libraries.

- `models.py` defines script-generation and speech-synthesis requests, results,
  metadata, and protocols.
- `errors.py` separates bounded-retry failures, fatal setup failures, and
  invalid backend output.

An adapter must keep its model-specific prompt format, subprocess arguments,
voice controls, and error translation behind these ports.

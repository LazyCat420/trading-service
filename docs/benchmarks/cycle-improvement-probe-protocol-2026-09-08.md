# Frozen follow-up probes

These are narrow mechanism checks, not a replacement for the original decision audit.
The task, expected answers, call limits and ordering are frozen in each probe script
before its first model call. All attempts are retained; no selective retries.

1. Tool recovery: two synthetic cases with a preceding `think` acknowledgement
   containing an intended market-data or calculator call, two repeats, counterbalanced
   baseline/current order. Four tool turns, 1,536 output tokens per call, 120s per call,
   GLM-5.3-Flash-EXL3, temperature 0. Current uses the exact adapter request transform.
   Success requires an actually executed fixture call, its receipt, and the correct value.
   No dispatch, database, or real external tool operations occur. Completed result:
   both arms 4/4 correct. These easy cases show no measured benefit.
2. Arithmetic/trigger handoff: four cases (cash decline, zero denominator, changed stop,
   fixed-price entry), two repeats, counterbalanced arm order. Same evidence and questions;
   the current arm adds the production arithmetic/trigger blocks. Four tool turns,
   3,072 output tokens per call, 120s per call, same model and sampling. Calculator
   schema is the frozen fixture; execution uses Decimal locally. Grade the three
   preregistered answer fields per case (numeric tolerance 0.001, exact booleans/nulls/
   strings), then separately review every final rationale for unsupported claims.
   Four cases are four independent units, regardless of repetition.

Provider usage is preserved. Missing usage is unknown, not zero. Shared GPU activity
precludes controlled speed comparisons. Both probes bypass the production Prism loop;
only the deployed cycle can validate the native tool interaction in that loop.


Setup correction: the first arithmetic-probe batch was rejected by the provider
before inference because the catalog omitted OpenAI's `type:function/function`
wrapper. All sixteen HTTP failures remain in the original output. The complete
cohort is restarted in a new output after correcting that wrapper and implementing
all advertised calculator operations. No model artifact was selected or discarded;
the setup failures are not scored as model reasoning failures.

# v4.6 egress capture feasibility

Status: implementation pending a decision on two conflicting requirements. This review did not modify production code, dependencies, verification, scoring, reporting, or v5 files.

## Decisions requiring approval

1. The requested MITM implementation cannot guarantee that the first TLS connection rejected by a pinned client still succeeds. Mitmproxy's own example records a failed handshake and bypasses interception on subsequent connections. Replaying a command is not a transparent workaround: arbitrary commands and requests may have side effects.
2. The unchanged evidence parser accepts origin-form HTTP/1.0 or HTTP/1.1 request bytes and final HTTP/1 response bytes. Native HTTP/2 frames cannot be passed to it. Mitmproxy's regular HTTP addon exposes parsed messages; its HTTP/1 assembly reconstructs header/chunk framing. Reconstructed messages cannot be described as original wire bytes. Normalizing evidence into HTTP/1 would require approval of this change in evidence meaning, even if the event's JSON keys stay identical.

Possible directions:

- Preserve strict non-interference and original-byte evidence: investigate passive capture, without compulsory TLS interception. Unsupported encrypted/protocol traffic stays unrecorded. This changes the requested architecture and is not yet implemented or validated.
- Retain the requested mitmproxy architecture: explicitly allow a first TLS failure before host bypass and allow reconstructed HTTP/1 evidence. These are changes to the user's mandatory constraints, not implementation details.

Sources:

- https://github.com/mitmproxy/mitmproxy/blob/main/examples/contrib/tls_passthrough.py
- https://github.com/mitmproxy/mitmproxy/blob/main/mitmproxy/net/http/http1/assemble.py
- Local code: engine/http_audit.py::_exchange_status, engine/http_capture.py::exchange_facts and response_body.

## Runtime integration findings

- The browser worker starts in start_session, before the per-exec CaptureSession. A shared proxy must start before browser launch and survive individual execs.
- Bind a flow at request start. Never attribute a delayed response to whichever exec happens to be active at response completion.
- Deactivation, queue draining, and event appending must be synchronized before exec_finished. Late or unattributable exchanges must remain unrecorded.
- Environment proxies cover cooperating clients, not arbitrary direct sockets. Do not block direct traffic to force capture.
- Preserve existing root certificates when adding run CA trust: replacing a trust bundle with only the run CA can break other HTTPS origins.
- Chromium trust requires its own implementation decision. Environment settings for Python/curl/Node are not evidence that Chromium trusts that CA. No browser was launched in this review.
- Proposed helper decision: keep helper APIs, but make recording mutually exclusive with proxy capture.
- Proposed library decision: mitmproxy rather than implementing a new HTTP/2/TLS stack, conditional on resolution of the conflicts above.
- Proposed origin decision: record target origin only; forward other origins without recording.
- Browser trust decision: pending architecture selection; do not silently replace CA trust with global certificate-error suppression.
- Current default Python is 3.10.0 on win32; Python 3.13 and 3.11 are also installed. Mitmproxy was not installed or run, so compatibility is not claimed.

## Executed checks

An in-memory check against the existing exchange_facts function produced:

| Input | Result |
| --- | --- |
| Origin-form HTTP/1 with JSON array | Accepted; status 200, json_counts {"": 0} |
| Chunked HTTP/1 with JSON array | Accepted; status 200, json_counts {"": 0} |
| Native HTTP/2 preface | Rejected: Expected an origin-form HTTP/1 request with one Host header |
| Absolute-form explicit HTTP proxy request | Rejected with the same error |

Command: `python -m unittest discover -s tests -p test_supported_observations.py -v`

Result: 25 tests ran in 38.765 seconds; 21 passed, 4 failed. All failures occurred in the existing worktree before any production edits in this task. Each failed because its runtime fixture could not import http_capture_client (ModuleNotFoundError):

- test_shape_v2_ignores_reflected_input_as_object_key
- test_shape_v2_keeps_array_length_comparison
- test_shape_v2_rejects_inconsistent_test_pair
- test_shape_v2_supports_repeated_security_question_object_difference_in_score

Passing tests included existing capture tamper rejection at import/recovery, scorer ordering, helper response contracts, seed capture replay, and historical journal input/context/hash preservation. These validate the existing helper-based implementation only; they do not validate a new proxy.

No full test suite, browser/UI validation, external target requests, CA installation, or proxy integration tests were performed.

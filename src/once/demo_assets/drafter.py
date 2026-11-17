"""The scripted drafter `once demo` uses. Replies in the shape `claude -p --output-format json` uses."""
import json
import sys

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
if "fixture" in prompt or "make check" in prompt:
    out = {"directive": "Run the tests with `make check`, never bare unittest or pytest: "
                        "the Makefile generates the fixtures the tests need.",
           "rationale": "Without it the agent ran the tests twice against missing fixtures.",
           "kind": "command", "path_globs": []}
else:
    out = {"skip": "nothing general"}
print(json.dumps({"result": json.dumps(out), "total_cost_usd": 0}))

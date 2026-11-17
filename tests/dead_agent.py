"""An agent that starts and immediately reports an error, as Claude Code does at a usage limit."""
import json

print(json.dumps({"is_error": True, "result": "Usage limit reached", "total_cost_usd": 0}))

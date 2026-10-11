"""Defaults and allowed values for the AI review gateway.

Nothing here pins a provider: the base URL, model, API style and the optional request
fields are all taken from the environment. The constants are the reference defaults.
"""

AI_MODEL = "deepseek-flash"
AI_BASE_URL = "https://api.deepseek.com"
AI_REASONING_EFFORT = "none"
# An empty value omits the field for providers that reject it.
AI_REASONING_EFFORTS = frozenset({"", "none", "minimal", "low", "medium", "high"})
AI_MAX_OUTPUT_TOKENS = 512
AI_SERVICE_TIER = "default"
AI_TIMEOUT_SECONDS = 12.0

AI_API_STYLE = "responses"
AI_API_STYLES = frozenset({"responses", "chat_completions"})
AI_STRUCTURED_OUTPUT = "json_schema"
AI_STRUCTURED_OUTPUTS = frozenset({"json_schema", "json_object"})

# Decisions made without calling the gateway; every other model name is a gateway call.
LOCAL_REVIEW_MODELS = frozenset({"local-signature", "local-media"})

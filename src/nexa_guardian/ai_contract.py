AI_MODEL = "gpt-5.6-luna"
AI_REASONING_EFFORT = "high"
AI_REASONING_EFFORTS = frozenset({"low", AI_REASONING_EFFORT})
AI_SERVICE_TIER = "default"
AI_TIMEOUT_SECONDS = 12.0

# Preserve historical model rows in cumulative AI statistics after model migrations.
AI_GATEWAY_MODELS = frozenset({"gpt-5.5", "gpt-5.6", "gpt-5.6-sol", AI_MODEL})

NEXA_OFFICIAL_PROPERTIES = (
    ("Nexa Store", "https://nexastore.cn", "nexastore.cn"),
    ("Nexa ModelPass", "https://openai.nexastore.cn", "openai.nexastore.cn"),
    ("Nexa GiftPass", "https://premium.nexastore.cn", "premium.nexastore.cn"),
)
NEXA_OFFICIAL_DOMAINS = frozenset(host for _, _, host in NEXA_OFFICIAL_PROPERTIES)

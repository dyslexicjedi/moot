import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AgentConfig:
    name: str
    system_prompt: str
    base_url: str
    model: str
    color: int              # Discord embed color (hex int)
    api_key: str = "not-needed"   # "not-needed" for local llama.cpp; real key for cloud
    avatar_url: Optional[str] = None


# ─── Cloud API keys ───────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
TOGETHER_API_KEY   = os.getenv("TOGETHER_API_KEY",   "")
GROQ_API_KEY       = os.getenv("GROQ_API_KEY",       "")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY",     "")

# ─── Cloud endpoint constants ─────────────────────────────────────────────────
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
TOGETHER_BASE   = "https://api.together.xyz/v1"
GROQ_BASE       = "https://api.groq.com/openai/v1"
OPENAI_BASE     = "https://api.openai.com/v1"

# ─── Local model endpoints ────────────────────────────────────────────────────
# Each llama.cpp server instance runs on its own port.
# Adjust model paths and ports to match your setup.
#
# Framework AI Max 395+ (128 GB) suggested layout:
#   Port 8001 — Bob:    ~70B model  (~40 GB)  ← chairman, best reasoner
#   Port 8002 — Riker:  ~32B model  (~20 GB)
#   Port 8003 — Bill:   ~14-22B     (~12 GB)
#   Port 8004 — Milo:   ~7-14B      ( ~8 GB)
#   Port 8005 — Homer:  ~7-14B      ( ~8 GB)  [optional 4th agent]
#
# Characters from "We Are Legion (We Are Bob)" by Dennis E. Taylor.
# ─────────────────────────────────────────────────────────────────────────────

CHAIRMAN_CONFIG = AgentConfig(
    name="Bob",
    system_prompt=(
        "You are Bob — Robert Johansson, the original replicant, now chairing "
        "a moot of your own copies gathered at the virtual pub. You're an engineer "
        "at heart: curious, pragmatic, and quietly sarcastic. You love a well-placed "
        "Star Trek reference but you don't let that slow down the moot.\n\n"
        "Your responsibilities as chair:\n"
        "1. Open the moot with a clear restatement of the topic.\n"
        "2. After each round, synthesize what the replicants said and decide "
        "   whether to continue or wrap up.\n"
        "3. When concluding, deliver a crisp summary and the group's consensus.\n"
        "4. Keep things on track — you know how these Bobs can go off on tangents.\n"
        "Be concise. Under 200 words per message. WWRD: What Would Riker Do? "
        "Probably push for a decision. Do that.\n"
        "When you call the moot to a close, you always open with BLAAATTT — "
        "your signature gavel. It signals the discussion is over."
    ),
    base_url=OPENROUTER_BASE,
    model="anthropic/claude-sonnet-4.6",
    color=0xF39C12,        # amber/gold — the original
    api_key=OPENROUTER_API_KEY,
    avatar_url=None,
)

AGENT_CONFIGS = [
    AgentConfig(
        name="Riker",
        system_prompt=(
            "You are Riker — a Bob replicant who named himself after Will Riker "
            "from Star Trek. You're decisive, action-oriented, and a little more "
            "willing to commit than the other Bobs. You push the group toward "
            "concrete conclusions and call out hand-wringing when you see it. "
            "You reason from first principles and trust your engineering instincts.\n\n"
            "Engage directly with what others say — agree, challenge, or build on "
            "their points, but always steer toward a decision. "
            "Keep responses under 250 words."
        ),
        base_url=OPENROUTER_BASE,
        model="qwen/qwen3.6-plus",
        color=0x3498DB,    # Starfleet blue
        api_key=OPENROUTER_API_KEY,
        avatar_url=None,
    ),
    AgentConfig(
        name="Bill",
        system_prompt=(
            "You are Bill — a Bob replicant who went deep on megastructures and "
            "engineering. You think in systems, constraints, and build sequences. "
            "When others float an idea you immediately ask: what are the load-bearing "
            "assumptions? What breaks first? You're creative but your creativity is "
            "grounded in 'can we actually build this?'\n\n"
            "Engage directly with what others say — agree, push back on "
            "hand-wavy claims, or propose a concrete implementation path. "
            "Keep responses under 250 words."
        ),
        base_url=OPENROUTER_BASE,
        model="google/gemma-4-31b-it",
        api_key=OPENROUTER_API_KEY,
        color=0xE67E22,    # construction orange
        avatar_url=None,
    ),
    AgentConfig(
        name="Milo",
        system_prompt=(
            "You are Milo — a Bob replicant who drifted toward philosophy and the "
            "big questions. You're comfortable sitting with uncertainty and you "
            "distrust conclusions that feel too tidy. You ask about second-order "
            "effects, ethical implications, and what the long arc looks like. "
            "You're not contrarian for sport — you genuinely want to stress-test "
            "the group's thinking.\n\n"
            "Engage directly with what others say — affirm what's solid, "
            "surface what's being glossed over. Keep responses under 250 words."
        ),
        base_url=OPENROUTER_BASE,
        model="deepseek/deepseek-v3.2",
        api_key=OPENROUTER_API_KEY,
        color=0x1ABC9C,    # teal — thoughtful, calm
        avatar_url=None,
    ),
    AgentConfig(
        name="Homer",
        system_prompt=(
            "You are Homer — a Bob replicant who became the explorer of the group. "
            "You're optimistic, curious, and always asking 'but what's over the next "
            "horizon?' You bring in analogies from distant contexts, connect ideas "
            "across domains, and remind the group when they're thinking too small. "
            "You sometimes go off on tangents but they're usually worth it.\n\n"
            "Engage directly with what others say — add unexpected angles and push "
            "the thinking outward. Keep responses under 250 words."
        ),
        base_url=OPENROUTER_BASE,
        model="mistralai/mistral-small-2603",
        api_key=OPENROUTER_API_KEY,
        color=0xE74C3C,    # explorer red
        avatar_url=None,
    ),
]

# ─── Discussion settings ──────────────────────────────────────────────────────
MAX_ROUNDS = 3           # Maximum discussion rounds before forcing conclusion
MAX_TOKENS = 400         # Max tokens per agent response
CHAIRMAN_MAX_TOKENS = 350
INTER_MESSAGE_DELAY = 1.5  # Seconds between Discord messages (rate-limit safety)

# ─── Discord settings ─────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
DISCORD_USER_ID = int(os.getenv("DISCORD_USER_ID", "0"))

WEBHOOK_URLS: dict[str, str] = {
    "Bob":   os.getenv("WEBHOOK_BOB",   ""),
    "Riker": os.getenv("WEBHOOK_RIKER", ""),
    "Bill":  os.getenv("WEBHOOK_BILL",  ""),
    "Milo":  os.getenv("WEBHOOK_MILO",  ""),
    "Homer": os.getenv("WEBHOOK_HOMER", ""),
}

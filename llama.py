import os
import json
import logging
from groq import AsyncGroq

log = logging.getLogger("automod.llama")

client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

SYSTEM_PROMPT = """You are a Discord server content moderation AI. Your job is to analyze messages and determine if they violate community guidelines.

You MUST respond ONLY with a valid JSON object — no explanation, no markdown, just JSON.

Analyze for:
- hate_speech: racial slurs, discrimination, targeted harassment based on identity
- toxic: insults, threats, extremely aggressive language, bullying
- spam: repetitive content, excessive caps, too many emojis, flooding, advertisement links

Response format:
{
  "flagged": true | false,
  "category": "hate_speech" | "toxic" | "spam" | "clean",
  "severity": "low" | "medium" | "high",
  "reason": "Brief explanation of why this was flagged, or 'Message is clean'"
}

Be accurate. Friendly banter and light profanity should NOT be flagged. Only flag genuinely harmful content."""


async def analyze_message(content: str) -> dict:
    """
    Send a message to Llama 3 via Groq for moderation analysis.
    Returns a dict with flagged, category, severity, reason.
    """
    try:
        response = await client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Analyze this message:\n\n{content}"},
            ],
            temperature=0.1,
            max_tokens=200,
        )

        raw = response.choices[0].message.content.strip()

        # Strip markdown code blocks if model wraps in ```json
        if raw.startswith("```"):
            raw = raw.strip("`").removeprefix("json").strip()

        result = json.loads(raw)
        log.info(f"LLM result: {result}")
        return result

    except json.JSONDecodeError as e:
        log.warning(f"JSON parse error from LLM: {e} — raw: {raw}")
        return {"flagged": False, "category": "clean", "severity": "low", "reason": "Parse error, defaulting to clean"}
    except Exception as e:
        log.error(f"Groq API error: {e}")
        return {"flagged": False, "category": "clean", "severity": "low", "reason": f"API error: {str(e)}"}

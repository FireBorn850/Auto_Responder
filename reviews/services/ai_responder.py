import os
import json
import re
from google import genai
from dotenv import load_dotenv


class QuotaExceededError(Exception):
    """Raised when Gemini returns a 429 RESOURCE_EXHAUSTED — lets callers
    show 'try again later' instead of a generic failure message."""
    pass


# Accented French vowels included so French reviews aren't mis-flagged
VOWELS = set('aeiouyàâäéèêëïîôöùûü')

SUPPORTED_LANGUAGES = {
    'fr': 'French',
    'en': 'English',
    'de': 'German',
    'it': 'Italian',
    'gsw': 'Swiss German (Schwiizerdütsch)',
}


def detect_review_language(comment: str) -> str:
    """
    Uses Gemini to detect which of our supported languages a review is
    written in — including distinguishing standard German from Swiss
    German dialect, which a simple word-list detector can't reliably do.
    Falls back to 'fr' (Geneva's default) if detection fails, since an
    unrecognized comment is more likely a data glitch than a genuinely
    unsupported language for a Geneva-based business.
    """
    text = (comment or '').strip()
    if not text:
        return 'fr'

    load_dotenv()
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return 'fr'

    client = genai.Client(api_key=api_key)

    lang_list = ', '.join(f'"{code}" ({name})' for code, name in SUPPORTED_LANGUAGES.items())
    prompt = f"""
    Identify which language this customer review is written in.

    Review Text: "{text}"

    Choose exactly ONE code from this list: {lang_list}.
    If the text mixes Swiss-German dialect spellings/vocabulary (e.g. "Chuchichäschtli", "grüezi", "merci vilmal") with standard German, choose "gsw" rather than "de".
    If uncertain between two close options, prefer the more common one for a Geneva restaurant/business review.

    Return ONLY a valid JSON object: {{"language": "<code>"}}
    """

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        if response and response.text:
            raw = response.text.strip()
            if "```" in raw:
                raw = re.sub(r'```(?:json)?\s*([\s\S]*?)\s*```', r'\1', raw).strip()
            data = json.loads(raw)
            code = data.get('language', 'fr')
            if code in SUPPORTED_LANGUAGES:
                return code
    except Exception:
        pass

    return 'fr'


def is_authentic_review(comment: str) -> bool:
    """
    Lightweight heuristic to catch keyboard-mash / gibberish input before
    it reaches the AI. Checks EACH WORD independently, not the whole
    comment averaged together — a single vowel-empty gibberish word (e.g.
    "kjtdthrgwshtgdfkjhgn") shouldn't be able to hide behind a second,
    coincidentally vowel-rich nonsense word in the same comment.
    """
    text = (comment or "").strip()
    if len(text) < 3:
        return False

    words = text.split()
    if not words:
        return False

    long_gibberish_words = 0
    long_words = 0

    for word in words:
        letters = [c.lower() for c in word if c.isalpha()]
        if len(letters) < 6:
            continue  # short words are too ambiguous to judge alone

        long_words += 1
        vowel_count = sum(1 for c in letters if c in VOWELS)
        vowel_ratio = vowel_count / len(letters)

        if vowel_ratio < 0.15:
            long_gibberish_words += 1

    # If there are no words long enough to judge, fall back to treating
    # the comment as authentic (short reviews like "Nul." or "Great!"
    # should never be flagged).
    if long_words == 0:
        return True

    # Flag as inauthentic if ANY sufficiently long word looks like
    # keyboard-mash — a real review very rarely contains even one.
    return long_gibberish_words == 0


def analyze_review_sentiment(comment, rating):
    """
    Uses Gemini to classify a review's true sentiment (which can differ
    from star rating — e.g. a sarcastic 5-star) and flag likely spam/fake
    content that the simpler is_authentic_review() heuristic might miss.
    """
    load_dotenv()
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return {'sentiment': 'neutral', 'is_likely_spam': False}

    client = genai.Client(api_key=api_key)

    prompt = f"""
    Analyze this customer review for a local business.

    Star Rating: {rating}/5
    Review Text: "{comment}"

    Return ONLY a valid JSON object with this exact structure:
    {{
        "sentiment": "positive" | "neutral" | "negative",
        "is_likely_spam": true | false
    }}

    Rules:
    - sentiment reflects the actual emotional tone of the text, which may contradict the star rating (e.g. sarcasm).
    - is_likely_spam is true only for bot-like, irrelevant, promotional, or nonsensical content — not for genuine negative feedback.
    """

    for model_name in ["gemini-3.6-flash"]:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={'response_mime_type': 'application/json'}
            )
            if response and response.text:
                raw = response.text.strip()
                if "```" in raw:
                    raw = re.sub(r'```(?:json)?\s*([\s\S]*?)\s*```', r'\1', raw).strip()
                data = json.loads(raw)
                return {
                    'sentiment': data.get('sentiment', 'neutral'),
                    'is_likely_spam': bool(data.get('is_likely_spam', False)),
                }
        except Exception:
            continue

    return {'sentiment': 'neutral', 'is_likely_spam': False}


def generate_review_draft(reviewer_name, star_rating, comment, language='fr', business_name="Geneva Bistro",
                           tone='friendly', custom_prompt='', signature='',
                           response_length='medium', creativity='medium', blacklisted_words='',
                           learned_patterns='', seo_keywords='', action_offer_label=''):
    """
    Generates an AI-drafted review reply using Gemini. Enforces blacklisted
    words two ways: (1) the model is told never to use them, and (2) the
    output is checked afterward — if a banned word slipped through anyway,
    it retries once with a stricter instruction before giving up.
    """
    load_dotenv()
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        print("[ai_responder] GEMINI_API_KEY is missing from environment variables.")
        return None

    client = genai.Client(api_key=api_key)

    tone_instructions = {
        'friendly': "Tone: Warm, welcoming, friendly, and grateful. Use a cheerful and conversational voice.",
        'professional': "Tone: Highly professional, polite, respectful, and formal. Maintain a polished business persona.",
        'casual': "Tone: Casual, upbeat, modern, and energetic. Feel free to be punchy and relatable."
    }
    selected_tone = tone_instructions.get(tone, tone_instructions['friendly'])

    length_instructions = {
        'short': "Keep the response very concise — 1 to 2 sentences maximum.",
        'medium': "Keep the response concise — 2 to 4 sentences maximum.",
        'long': "Write a fuller, more detailed response — 4 to 6 sentences.",
    }
    selected_length = length_instructions.get(response_length, length_instructions['medium'])

    temperature_map = {'low': 0.3, 'medium': 0.7, 'high': 1.1}
    selected_temperature = temperature_map.get(creativity, 0.7)

    banned_words = [w.strip() for w in (blacklisted_words or '').split(',') if w.strip()]

    custom_context_block = ""
    if custom_prompt.strip():
        custom_context_block = f"""
    6. Business-Specific Context & Rules (follow these when relevant to the review):
       {custom_prompt.strip()}
    """

    blacklist_block = ""
    if banned_words:
        blacklist_block = f"""
    7. Forbidden Words: Under no circumstances use any of the following words or phrases, in any language, in any form: {', '.join(banned_words)}.
    """
    learned_patterns_block = ""
    if learned_patterns and learned_patterns.strip():
        learned_patterns_block = f"""
    8. Learned Style (based on how this owner has edited past AI drafts — match this from the start): {learned_patterns.strip()}
    """

    seo_keywords_list = [w.strip() for w in (seo_keywords or '').split(',') if w.strip()]
    seo_block = ""
    if seo_keywords_list:
        seo_block = f"""
    9. Local SEO Reinforcement (OPTIONAL — use only if it fits naturally): If, and only if, the review's own content gives a genuine, natural opening, you may weave in ONE of these phrases exactly as written, used at most once: {', '.join(seo_keywords_list)}. Do NOT force one in if nothing in the review relates to it — a reply with none of these phrases is completely normal and expected for most reviews. Never let this compromise Rule 3 (sentiment strategy) or make the reply sound like keyword stuffing — it must read exactly like something a real manager would naturally say.
    """

    action_block = ""
    if action_offer_label and action_offer_label.strip():
        action_block = f"""
    10. Warm Invitation: Since this is a happy customer, end with one brief, natural sentence inviting them to check out {action_offer_label.strip()}. Do NOT include a URL or link yourself — one will be appended automatically after your response. Just mention it warmly, like a manager casually telling a regular about something new.
    """



    language_name = SUPPORTED_LANGUAGES.get(language, 'French')
    language_nuance_notes = {
        'fr': "Write in standard Swiss French — natural for Geneva, not Parisian slang.",
        'en': "Write in clear, warm international English — the reviewer may be a tourist.",
        'de': "Write in standard High German (Hochdeutsch), polite register — appropriate for a Swiss-German-speaking visitor, not a Geneva local.",
        'it': "Write in standard Italian, warm and polite register.",
        'gsw': "The review was written in Swiss German dialect. Reply in standard High German (Hochdeutsch) rather than attempting to write dialect yourself, since dialect spelling varies by canton and a mismatched dialect can feel more off than standard German. Keep the tone as warm as the dialect original suggested.",
    }
    selected_nuance = language_nuance_notes.get(language, language_nuance_notes['fr'])

    language_name = SUPPORTED_LANGUAGES.get(language, 'French')
    language_nuance_notes = {
        'fr': "Write in standard Swiss French — natural for Geneva, not Parisian slang.",
        'en': "Write in clear, warm international English — the reviewer may be a tourist.",
        'de': "Write in standard High German (Hochdeutsch), polite register — appropriate for a Swiss-German-speaking visitor, not a Geneva local.",
        'it': "Write in standard Italian, warm and polite register.",
        'gsw': "The review was written in Swiss German dialect. Reply in standard High German (Hochdeutsch) rather than attempting to write dialect yourself, since dialect spelling varies by canton and a mismatched dialect can feel more off than standard German. Keep the tone as warm as the dialect original suggested.",
    }
    selected_nuance = language_nuance_notes.get(language, language_nuance_notes['fr'])

    def build_prompt(extra_warning=""):
        return f"""
    You are the customer relations manager for "{business_name}", a local establishment in Geneva, Switzerland.

    Task: Draft a response to this Google Review following the specified brand voice and guidelines.

    Review Details:
    - Customer Name: {reviewer_name}
    - Rating: {star_rating} out of 5 stars
    - Review Comment: "{comment}"

    Guidelines:
    1. Language Rule: Draft the reply strictly in {language_name}. {selected_nuance}
    2. Voice/Tone Rule: {selected_tone}
    3. Sentiment Strategy:
       - If rating is 4 or 5 stars: Express warm gratitude and thank them for visiting.
       - If rating is 1, 2, or 3 stars: Be empathetic, apologize sincerely, avoid being defensive, and politely ask them to contact us directly at azizovjasur2007@gmail.com so we can resolve it offline.
    4. Length Rule: {selected_length}
    5. Output ONLY the response text. Do not include markdown headers, meta instructions, or a signature line — that gets appended separately.
    {custom_context_block}{blacklist_block}{learned_patterns_block}{seo_block}{action_block}{extra_warning}
    """

    def violates_blacklist(text: str) -> str | None:
        lowered = text.lower()
        for word in banned_words:
            if word.lower() in lowered:
                return word
        return None

    available_models = ["gemini-3.6-flash"]
    last_error = ""

    for attempt in range(2):  # first try, then one stricter retry if a banned word slips through
        extra_warning = ""
        if attempt == 1:
            extra_warning = f"\n    IMPORTANT: Your previous draft used a forbidden word. Rewrite it completely, avoiding: {', '.join(banned_words)}."

        prompt = build_prompt(extra_warning)

        for model_name in available_models:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config={'temperature': selected_temperature}
                )
                if response and response.text:
                    draft = response.text.strip()

                    if banned_words:
                        hit = violates_blacklist(draft)
                        if hit:
                            last_error = f"Draft used forbidden word '{hit}'"
                            continue  # try next model / retry loop

                    if signature.strip():
                        draft = f"{draft}\n\n{signature.strip()}"
                    return draft
            except Exception as e:
                last_error = str(e)
                if '429' in last_error or 'RESOURCE_EXHAUSTED' in last_error:
                    print(f"[ai_responder] Gemini quota exceeded: {last_error}")
                    raise QuotaExceededError(last_error)
                continue

    print(f"[ai_responder] Gemini generation failed for all models/attempts. Last error: {last_error}")
    return None


def append_action_link(draft_text: str, url: str, label: str) -> str:
    """
    Deterministically appends the real, exact link after generation —
    never trusted to the AI, so it can never be broken, hallucinated, or
    mistyped. Mirrors how the signature field is appended.
    """
    return f"{draft_text}\n\n👉 {label.strip()}: {url.strip()}"


def detect_seo_keyword_used(draft_text: str, seo_keywords: str) -> str:
    """
    Checks whether the AI's draft reply naturally included one of the
    business's target local-SEO phrases. Pure post-hoc substring check —
    no extra API call, so this costs nothing to run. Returns the matched
    phrase (in the owner's original casing) or '' if none was used.
    """
    if not draft_text or not seo_keywords:
        return ''
    lowered_draft = draft_text.lower()
    for phrase in [p.strip() for p in seo_keywords.split(',') if p.strip()]:
        if phrase.lower() in lowered_draft:
            return phrase
    return ''


def summarize_edit_patterns(pairs):
    """
    Given [{'draft': ai_text, 'final': human_edited_text}, ...], asks Gemini
    to summarize the RECURRING style differences as plain writing guidance.
    Returns a short string ready to inject into future prompts, or None.
    """
    if not pairs:
        return None

    load_dotenv()
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return None

    client = genai.Client(api_key=api_key)

    formatted_pairs = "\n\n".join(
        f'AI Draft: "{p["draft"]}"\nPublished: "{p["final"]}"' for p in pairs[:20]
    )

    prompt = f"""
    Below are recent pairs of (AI-drafted review reply -> what the business owner actually published):

    {formatted_pairs}

    Identify the 3-5 most CONSISTENT, recurring patterns in how the owner changes drafts
    (e.g. shortens length, drops exclamation points, adds a specific phrase, changes formality).
    Ignore one-off edits that don't repeat across multiple pairs.

    Output ONLY a short paragraph (2-4 sentences) of direct writing-style guidance a writer
    could follow to match this owner's voice from the first draft. Do not mention "AI" or "draft".
    """

    for model_name in ["gemini-3.6-flash"]:
        try:
            response = client.models.generate_content(model=model_name, contents=prompt)
            if response and response.text:
                return response.text.strip()
        except Exception:
            continue

    return None


def analyze_complaints(comments_list):
    """
    Analyzes a list of negative review comments, clusters recurring complaints,
    and returns a structured JSON summary with keywords, counts, and recommendations.
    """
    if not comments_list:
        return {
            "summary": "No negative reviews found.",
            "top_issues": [],
            "actionable_tip": "Keep up the excellent service!"
        }

    load_dotenv()
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return {"summary": "API Key Missing", "top_issues": []}

    client = genai.Client(api_key=api_key)

    formatted_comments = "\n".join([f"- {c}" for c in comments_list])

    prompt = f"""
    You are an expert customer experience analyst.
    Analyze the following list of negative customer review comments (1-3 stars) and cluster recurring complaints into clear categories.

    Review Comments:
    {formatted_comments}

    Analyze the recurring themes and return a valid JSON object matching this exact structure:
    {{
        "summary": "Brief 1-2 sentence overall diagnosis of the negative feedback.",
        "top_issues": [
            {{
                "category": "Short title (e.g. Slow Service, Food Quality, Pricing)",
                "mentions_count": 1,
                "severity": "High",
                "sample_quote": "A short representative snippet from review"
            }}
        ],
        "actionable_tip": "One clear, specific improvement recommendation for the owner."
    }}
    IMPORTANT: Respond ONLY with raw JSON text. Do not wrap in markdown or backticks.
    """

    available_models = ["gemini-3.6-flash"]
    last_error = ""

    for model_name in available_models:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={'response_mime_type': 'application/json'}
            )

            if response and response.text:
                raw_text = response.text.strip()

                if "```" in raw_text:
                    raw_text = re.sub(r'```(?:json)?\s*([\s\S]*?)\s*```', r'\1', raw_text).strip()

                return json.loads(raw_text)

        except json.JSONDecodeError as e:
            last_error = f"Malformed JSON from model: {e}. Raw response: {raw_text[:300] if 'raw_text' in dir() else 'N/A'}"
            print(f"[ai_responder] analyze_complaints JSON parse failed: {last_error}")
            continue
        except Exception as e:
            last_error = str(e)
            if '429' in last_error or 'RESOURCE_EXHAUSTED' in last_error:
                print(f"[ai_responder] analyze_complaints quota exceeded: {last_error}")
                raise QuotaExceededError(last_error)
            print(f"[ai_responder] analyze_complaints failed for model '{model_name}': {last_error}")
            continue

    print(f"[ai_responder] analyze_complaints exhausted all models/attempts. Last error: {last_error}")
    return {
        "summary": "Feedback recorded, but automated analysis encountered an issue.",
        "top_issues": [
            {
                "category": "Customer Feedback",
                "mentions_count": len(comments_list),
                "severity": "Medium",
                "sample_quote": comments_list[0] if comments_list else "Customer reported an issue."
            }
        ],
        "actionable_tip": "Review recent negative feedback manually while the system refreshes."
    }
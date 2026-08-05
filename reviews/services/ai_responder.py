import os
import json
import re
from google import genai
from dotenv import load_dotenv


# Accented French vowels included so French reviews aren't mis-flagged
VOWELS = set('aeiouyàâäéèêëïîôöùûü')


def is_authentic_review(comment: str) -> bool:
    """
    Lightweight heuristic to catch keyboard-mash / gibberish input before
    it reaches the AI. Real words (in French or English) have a normal
    vowel-to-letter ratio; random mashing typically doesn't.

    This intentionally does NOT flag short-but-real reviews like
    "not good at all" or "Nul." — only text that reads as nonsense.
    """
    text = (comment or "").strip()
    if len(text) < 3:
        return False

    letters = [c.lower() for c in text if c.isalpha()]
    if not letters:
        return False

    vowel_count = sum(1 for c in letters if c in VOWELS)
    vowel_ratio = vowel_count / len(letters)

    # Long run of letters with almost no vowels = very likely gibberish
    if len(letters) >= 6 and vowel_ratio < 0.15:
        return False

    return True


def generate_review_draft(reviewer_name, star_rating, comment, language='fr', business_name="Geneva Bistro", tone='friendly'):
    """
    Generates an AI-drafted review reply using Gemini, based on brand tone and language.
    """
    load_dotenv()
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return "Error: GEMINI_API_KEY is missing from environment variables."

    client = genai.Client(api_key=api_key)

    tone_instructions = {
        'friendly': "Tone: Warm, welcoming, friendly, and grateful. Use a cheerful and conversational voice.",
        'professional': "Tone: Highly professional, polite, respectful, and formal. Maintain a polished business persona.",
        'casual': "Tone: Casual, upbeat, modern, and energetic. Feel free to be punchy and relatable."
    }

    selected_tone = tone_instructions.get(tone, tone_instructions['friendly'])

    prompt = f"""
    You are the customer relations manager for "{business_name}", a local establishment in Geneva, Switzerland.

    Task: Draft a response to this Google Review following the specified brand voice and guidelines.

    Review Details:
    - Customer Name: {reviewer_name}
    - Rating: {star_rating} out of 5 stars
    - Review Comment: "{comment}"

    Guidelines:
    1. Language Rule: Draft the reply strictly in {language.upper()} ('fr' = French, 'en' = English).
    2. Voice/Tone Rule: {selected_tone}
    3. Sentiment Strategy:
       - If rating is 4 or 5 stars: Express warm gratitude and thank them for visiting.
       - If rating is 1, 2, or 3 stars: Be empathetic, apologize sincerely, avoid being defensive, and politely ask them to contact us directly at support@business.ch so we can resolve it offline.
    4. Keep the response concise (2 to 4 sentences maximum).
    5. Output ONLY the response text. Do not include markdown headers or meta instructions.
    """

    available_models = ["gemini-3.5-flash", "gemini-2.5-flash"]

    last_error = ""
    for model_name in available_models:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            last_error = str(e)
            continue

    return f"Error: Could not generate response. Last error: {last_error}"


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

    available_models = ["gemini-3.5-flash", "gemini-2.5-flash"]

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

        except Exception:
            continue

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
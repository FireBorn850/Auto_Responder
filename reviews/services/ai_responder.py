import os
from google import genai

def generate_review_draft(reviewer_name, star_rating, comment, language='fr', business_name="Geneva Bistro"):
    """
    Dynamically finds an active model from your API account and generates the review reply.
    """
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return "Error: GEMINI_API_KEY is missing from environment variables."

    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are the customer relations manager for "{business_name}", a local establishment in Geneva, Switzerland.
    
    Task: Draft a professional, warm, and appropriate response to this Google Review.
    
    Review Details:
    - Customer Name: {reviewer_name}
    - Rating: {star_rating} out of 5 stars
    - Review Comment: "{comment}"
    
    Guidelines:
    1. Language Rule: Draft the reply strictly in {language.upper()} ('fr' = French, 'en' = English).
    2. Tone Rule:
       - If rating is 4 or 5 stars: Be warm, enthusiastic, and thank them for visiting.
       - If rating is 1, 2, or 3 stars: Be empathetic, apologize for the poor experience, and politely ask them to contact us directly at support@business.ch so we can resolve it.
    3. Keep the response concise (2 to 4 sentences maximum).
    4. Output ONLY the response text. Do not include markdown headers or meta instructions.
    """

    # 1. Fetch available models directly from your Google API account
    available_models = []
    try:
        for m in client.models.list():
            # Extract clean model name (e.g., 'gemini-2.0-flash' or 'gemini-1.5-flash')
            name = m.name.replace("models/", "") if hasattr(m, "name") else str(m)
            if "flash" in name or "pro" in name:
                available_models.append(name)
    except Exception:
        # Fallback list if listing fails
        available_models = ["gemini-2.0-flash", "gemini-1.5-flash"]

    # 2. Try generation on available models
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
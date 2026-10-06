"""
Free public "AI reply to a review" tool (lead magnet + SEO page).

Two addresses, one per language, so Google can rank each one:
  /free-review-reply-generator/   (English)
  /repondre-avis-google/          (French)

Cost guard: every reply is a Gemini call, so it is capped per visitor and
for the whole site per day. Nothing is stored.
"""
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .services import ratelimit
from .services.client_ip import get_client_ip
from .services.language import guess_language
from .services.ai_responder import generate_review_draft, is_authentic_review, QuotaExceededError

PER_VISITOR_PER_DAY = 5
SITE_PER_DAY = 150
DAY = 86400

TEXT = {
    'en': {
        'title': 'Free AI Review Reply Generator for Restaurants | Mehrly',
        'description': 'Paste a Google review and get a professional, personal reply in seconds, in the guest\'s language. Free, no sign-up. Made for restaurants, cafés and hotels.',
        'h1': 'Free AI reply generator for Google reviews',
        'intro': 'Paste a customer review, and get a ready-to-post, personal reply in a few seconds. Works for good and bad reviews, in the guest\'s language. Free, no sign-up.',
        'business': 'Your business name (optional)',
        'business_ph': 'e.g. Café du Lac',
        'reviewer': 'Customer name (optional)',
        'reviewer_ph': 'e.g. Sarah',
        'rating': 'Star rating',
        'review': 'The review',
        'review_ph': 'Paste the customer\'s review here…',
        'tone': 'Tone',
        'tone_friendly': 'Warm & friendly',
        'tone_professional': 'Professional',
        'button': 'Write my reply',
        'working': 'Writing your reply…',
        'result': 'Your reply, ready to copy',
        'copy': 'Copy',
        'copied': 'Copied!',
        'limit_note': 'Free: %(n)s replies per day.' % {'n': PER_VISITOR_PER_DAY},
        'cta_h': 'Want this for every new review, automatically?',
        'cta_p': 'Mehrly imports your Google reviews and prepares a reply for each one, in your tone. You approve in one click.',
        'cta_btn': 'Start free trial',
        'faq_h': 'Questions',
        'faq': [
            ('Is it really free?', 'Yes. You can generate a few replies per day without an account. If you want replies for every new review automatically, Mehrly has a 14-day free trial.'),
            ('Which languages does it support?', 'It detects the language of the review and answers in the same language: French, English, German, Italian, Swiss German and more.'),
            ('Should I reply to negative reviews?', 'Yes. A calm, personal reply shows future guests that you listen. Thank the guest, acknowledge the problem, apologise sincerely and invite them back. Avoid arguing in public.'),
            ('Do you store my review?', 'No. The text is only used to write the reply and is not saved.'),
        ],
        'err_generic': 'Something went wrong, please try again.',
        'other_lang': ('Français', '/repondre-avis-google/'),
    },
    'fr': {
        'title': 'Générateur gratuit de réponses aux avis Google (IA) | Mehrly',
        'description': 'Collez un avis Google et obtenez en quelques secondes une réponse professionnelle et personnelle, dans la langue du client. Gratuit, sans inscription. Pour restaurants, cafés et hôtels.',
        'h1': 'Répondre à un avis Google : générateur gratuit',
        'intro': 'Collez l’avis d’un client et obtenez en quelques secondes une réponse personnelle, prête à publier. Pour les bons comme les mauvais avis, dans la langue du client. Gratuit, sans inscription.',
        'business': 'Nom de votre établissement (facultatif)',
        'business_ph': 'ex. Café du Lac',
        'reviewer': 'Prénom du client (facultatif)',
        'reviewer_ph': 'ex. Sarah',
        'rating': 'Note',
        'review': 'L’avis',
        'review_ph': 'Collez ici l’avis du client…',
        'tone': 'Ton',
        'tone_friendly': 'Chaleureux',
        'tone_professional': 'Professionnel',
        'button': 'Écrire ma réponse',
        'working': 'Rédaction en cours…',
        'result': 'Votre réponse, prête à copier',
        'copy': 'Copier',
        'copied': 'Copié !',
        'limit_note': 'Gratuit : %(n)s réponses par jour.' % {'n': PER_VISITOR_PER_DAY},
        'cta_h': 'Une réponse pour chaque nouvel avis, automatiquement ?',
        'cta_p': 'Mehrly importe vos avis Google et prépare une réponse pour chacun, dans votre style. Vous validez en un clic.',
        'cta_btn': 'Essai gratuit',
        'faq_h': 'Questions fréquentes',
        'faq': [
            ('C’est vraiment gratuit ?', 'Oui. Vous pouvez générer quelques réponses par jour sans compte. Pour une réponse automatique à chaque nouvel avis, Mehrly propose 14 jours d’essai gratuit.'),
            ('Quelles langues ?', 'L’outil détecte la langue de l’avis et répond dans la même langue : français, anglais, allemand, italien, suisse allemand et plus.'),
            ('Faut-il répondre aux avis négatifs ?', 'Oui. Une réponse calme et personnelle montre aux futurs clients que vous écoutez. Remerciez, reconnaissez le problème, excusez-vous sincèrement et invitez le client à revenir. Évitez de vous disputer en public.'),
            ('Mon avis est-il enregistré ?', 'Non. Le texte sert uniquement à écrire la réponse et n’est pas conservé.'),
        ],
        'err_generic': 'Une erreur est survenue, veuillez réessayer.',
        'other_lang': ('English', '/free-review-reply-generator/'),
    },
}

ERRORS = {
    'en': {
        'limit': "You've used today's free replies. Come back tomorrow, or start a free trial to reply to every review.",
        'busy': 'The free tool is very busy today. Please try again tomorrow.',
        'empty': 'Please paste a review first.',
        'fake': "That doesn't look like a real customer review. Please paste a real one.",
    },
    'fr': {
        'limit': 'Vous avez utilisé vos réponses gratuites du jour. Revenez demain, ou essayez Mehrly gratuitement pour répondre à chaque avis.',
        'busy': 'L’outil gratuit est très sollicité aujourd’hui. Réessayez demain.',
        'empty': 'Collez d’abord un avis.',
        'fake': 'Cela ne ressemble pas à un vrai avis client. Collez un vrai avis.',
    },
}


@require_http_methods(['GET', 'POST'])
def free_reply_tool_view(request, lang='en'):
    lang = 'fr' if lang == 'fr' else 'en'
    if request.method == 'GET':
        return render(request, 'reviews/free_reply_tool.html', {
            't': TEXT[lang], 'page_lang': lang,
            'alt_en': '/free-review-reply-generator/', 'alt_fr': '/repondre-avis-google/',
        })

    err = ERRORS[lang]
    comment = (request.POST.get('comment') or '').strip()[:1500]
    if not comment:
        return JsonResponse({'error': err['empty']}, status=400)
    if not is_authentic_review(comment):
        return JsonResponse({'error': err['fake']}, status=400)

    ip = get_client_ip(request)
    if ratelimit.too_many(f'freetool:{ip}', PER_VISITOR_PER_DAY, DAY):
        return JsonResponse({'error': err['limit'], 'limit': True}, status=429)
    if ratelimit.too_many('freetool:all', SITE_PER_DAY, DAY):
        return JsonResponse({'error': err['busy']}, status=429)
    ratelimit.hit(f'freetool:{ip}', DAY)
    ratelimit.hit('freetool:all', DAY)

    try:
        rating = max(1, min(5, int(request.POST.get('rating', 5))))
    except (TypeError, ValueError):
        rating = 5
    tone = request.POST.get('tone') if request.POST.get('tone') in ('friendly', 'professional') else 'friendly'
    reviewer = (request.POST.get('reviewer_name') or '').strip()[:60] or ('Client' if lang == 'fr' else 'Guest')
    business = (request.POST.get('business_name') or '').strip()[:80] or ('notre établissement' if lang == 'fr' else 'our place')

    try:
        draft = generate_review_draft(
            reviewer_name=reviewer, star_rating=rating, comment=comment,
            language=guess_language(comment), business_name=business,
            tone=tone, custom_prompt='', signature='',
            response_length='medium', creativity='medium', blacklisted_words='',
        )
    except QuotaExceededError:
        return JsonResponse({'error': err['busy']}, status=429)
    if not draft:
        return JsonResponse({'error': TEXT[lang]['err_generic']}, status=502)
    return JsonResponse({'reply': draft})

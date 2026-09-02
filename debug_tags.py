import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.template.base import Lexer

path = 'reviews/templates/reviews/settings.html'
content = open(path, encoding='utf-8').read()
tokens = Lexer(content).tokenize()

for t in tokens:
    if 780 <= t.lineno <= 792:
        print(f"line {t.lineno} | type={t.token_type.name:5} | repr={t.contents!r}")
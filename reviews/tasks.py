from celery import shared_task
from allauth.socialaccount.models import SocialToken, SocialAccount
import requests

@shared_task
def poll_google_reviews():
    # Fetch all users who connected their account via Google OAuth
    google_accounts = SocialAccount.objects.filter(provider='google')
    
    for account in google_accounts:
        try:
            # Retrieve the saved Google access token for this user
            token = SocialToken.objects.get(account=account)
            access_token = token.token
            
            # Request business accounts list from Google API
            headers = {'Authorization': f'Bearer {access_token}'}
            url = 'https://mybusinessaccountmanagement.googleapis.com/v1/accounts'
            
            response = requests.get(url, headers=headers)
            
            if response.status_code == 200:
                print(f"[SUCCESS] Polled Google API for {account.user.username}: {response.json()}")
                # Here you will process reviews or save them to your database
            else:
                print(f"[ERROR] Failed for {account.user.username}: {response.status_code} - {response.text}")

        except SocialToken.DoesNotExist:
            print(f"[WARNING] No OAuth token found for {account.user.username}")
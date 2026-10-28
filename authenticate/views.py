import os
from django.shortcuts import render, redirect
from django.urls import reverse
import google_auth_oauthlib.flow
import requests
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

from .models import AuthorizedUser
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from helpers import rate_limit, login_required
import json

# Create your views here.
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'  # nosec B105

_DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/userinfo.profile',
          'https://www.googleapis.com/auth/userinfo.email']

client_config = {
    "web": {
        "client_id": os.getenv("GOOGLE_CLIENT_ID", "").strip(),
        "project_id": "scouting-excel-test",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",  # nosec B105
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
        "redirect_uris": [
            "http://localhost:8000/auth/oauth2callback/",
            "http://127.0.0.1:8000/auth/oauth2callback/",
            "https://scouting.team2073.com/auth/oauth2callback/",
            "https://scouting.chrisccluk.live/auth/oauth2callback/",
            "https://scout.chrisccluk.live/auth/oauth2callback/"
        ],
        "javascript_origins": [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "https://scouting.team2073.com",
            "https://scouting.chrisccluk.live",
            "https://scout.chrisccluk.live"
        ]
    }
}


def authorize(request):
    # If user is already authenticated, redirect to home
    if request.session.get('email'):
        return redirect('/')
    
    # Check if this is a direct auth request (from login button)
    if request.GET.get('login') == 'true':
        if _DEBUG:
            os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
        try:
            flow = google_auth_oauthlib.flow.Flow.from_client_config(
                client_config=client_config, scopes=SCOPES)

            flow.redirect_uri = request.build_absolute_uri(reverse('oauth2callback'))

            authorization_url, state = flow.authorization_url(
                access_type='offline',
                include_granted_scopes='true')

            # Store state and PKCE code_verifier in session to verify/reuse on callback
            request.session['oauth_state'] = state
            request.session['oauth_code_verifier'] = flow.code_verifier

            return redirect(authorization_url)
        finally:
            os.environ.pop('OAUTHLIB_INSECURE_TRANSPORT', None)
    
    # Otherwise show login page
    return render(request, 'authenticate/login.html')


def oauth2callback(request):
    # Verify OAuth state to prevent login CSRF
    expected_state = request.session.get('oauth_state')
    returned_state = request.GET.get('state')
    if not expected_state or expected_state != returned_state:
        return render(request, 'authenticate/unauthorized.html', {'email': 'OAuth state mismatch — possible CSRF attack'})

    if _DEBUG:
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        client_config=client_config, scopes=SCOPES, state=expected_state,
        code_verifier=request.session.get('oauth_code_verifier'))
    flow.redirect_uri = request.build_absolute_uri(reverse('oauth2callback'))

    # Use the authorization server's response to fetch the OAuth 2.0 tokens.
    authorization_response = request.build_absolute_uri()
    try:
        flow.fetch_token(authorization_response=authorization_response)
    except (requests.exceptions.RequestException, OAuth2Error):
        if _DEBUG:
            os.environ.pop('OAUTHLIB_INSECURE_TRANSPORT', None)
        return render(request, 'authenticate/unauthorized.html', {'email': 'Login failed — please try again'})

    # Single-use values — clear now that the token exchange succeeded
    request.session.pop('oauth_state', None)
    request.session.pop('oauth_code_verifier', None)

    if _DEBUG:
        os.environ.pop('OAUTHLIB_INSECURE_TRANSPORT', None)

    # Store credentials in the session.
    # ACTION ITEM: In a production app, you likely want to save these
    #              credentials in a persistent database instead.
    credentials = flow.credentials
    cred = credentials_to_dict(credentials)
    request.session['credentials'] = cred

    try:
        r = requests.get('https://www.googleapis.com/oauth2/v2/userinfo',
                         headers={'Authorization': f'Bearer {cred["token"]}'},
                         timeout=10).json()
    except (requests.exceptions.RequestException, ValueError):
        return render(request, 'authenticate/unauthorized.html', {'email': 'Login failed — please try again'})

    given  = r.get("given_name", "")
    family = r.get("family_name", "")
    request.session["name"] = (given + " " + family).strip() or r.get("name", "User")

    email = r.get("email")
    if not email:
        return render(request, 'authenticate/unauthorized.html', {'email': 'Login failed — please try again'})

    email = email.strip().lower()
    authorized_user = AuthorizedUser.objects.filter(email__iexact=email).first()
    if authorized_user or email.endswith('@team2073.com'):
        is_authorized = True
    else:
        return render(request, 'authenticate/unauthorized.html', {'email': email})

    roles = authorized_user.get_roles_list() if authorized_user else []
    role = ','.join(roles) or 'student'
    is_admin = bool(authorized_user and authorized_user.is_admin)

    request.session["email"] = email
    request.session["is_authorized"] = is_authorized
    request.session["is_admin"] = is_admin
    request.session["role"] = role

    return redirect('/')


@login_required
@require_POST
def logout(request):
    request.session.flush()
    return redirect('/auth/')


@login_required
@require_POST
def set_theme(request):
    """Set user's theme preference in session (expects JSON {inverted: true/false})"""
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
        inverted = bool(payload.get('inverted', False))
    except Exception:
        # Fallback to form-encoded
        inverted = request.POST.get('inverted') in ['1', 'true', 'True']

    request.session['inverted'] = inverted
    return JsonResponse({'status': 'success', 'inverted': inverted})


def credentials_to_dict(credentials):
    # Only store the access token needed for userinfo API calls.
    # Never store client_secret or refresh_token in the session.
    return {'token': credentials.token}
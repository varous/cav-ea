"""Scripted OAuth walkthrough for the assistant's user grants.

Two unavoidable manual pieces remain (they need the owner's browser):

1. In the Google Cloud Console, create/confirm the Google Chat app (API & Services
   configuration) and a **Desktop** OAuth client for the owner account.
2. Grant the Workspace Events publisher on the events topic (see README).

This script prints those steps, then (where possible) runs a Desktop consent flow,
stores the resulting user JSON in a Secret Manager secret **without printing any
token**, and verifies the assistant with a single ``POST /maintain``.

Examples::

    python scripts/oauth_setup.py --kind chat  --secret ea-chat-oauth  --client client.json --project your-project
    python scripts/oauth_setup.py --kind gmail --secret ea-gmail-oauth --client client.json --project your-project
    python scripts/oauth_setup.py --kind drive --secret ea-collector-drive-oauth --client client.json --project your-project
    python scripts/oauth_setup.py --dry-run --kind chat
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import urllib.request

SCOPES = {
    "chat": [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
        "https://www.googleapis.com/auth/chat.messages.readonly",
    ],
    "gmail": [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/gmail.readonly",
    ],
    "drive": [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/drive.file",
    ],
    "tasks": [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/tasks",
    ],
}


def scopes_for(kind):
    if kind not in SCOPES:
        raise ValueError("UNKNOWN_KIND:" + kind)
    return list(SCOPES[kind])


def console_steps(kind):
    return [
        "In Google Cloud Console, enable the Google Chat API and configure the Chat app "
        "for the space (app name, avatar, and the assistant service account as the app identity).",
        "Create a Desktop OAuth client (APIs & Services > Credentials). Download the client JSON "
        "to a local path — never commit it.",
        "Run this script with --kind " + kind + " --client <client.json> --secret <secret-name>. "
        "It opens a browser consent for the owner account only; no other account is accepted.",
        "Grant the Workspace Events publisher on the events topic (README manual step).",
        "Verify the assistant: scripts/oauth_setup.py ... --verify-url https://<service>/maintain",
    ]


def store_secret(secret, path, project, runner=subprocess.run):
    """Add a secret version from a file. The file contents are never printed."""
    argv = ["gcloud", "secrets", "versions", "add", secret,
            "--data-file=" + path, "--project=" + project, "--quiet"]
    runner(argv, check=True)
    return argv


def write_user_json(credentials):
    """Write credentials to a 0600 temp file. Returns the path (caller deletes it)."""
    handle, path = tempfile.mkstemp(suffix=".json", prefix="ea-oauth-")
    os.close(handle)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(credentials.to_json())
    return path


def run_consent(client, scopes, login_hint, port=0):
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client, scopes)
    return flow.run_local_server(
        port=port,
        login_hint=login_hint,
        access_type="offline",
        prompt="select_account consent",
        authorization_prompt_message="",
        timeout_seconds=1200,
    )


def verify_maintain(url, token=None, opener=urllib.request.urlopen):
    """POST /maintain once. Only the HTTP status is returned/printed."""
    if token is None:
        token = subprocess.check_output(["gcloud", "auth", "print-identity-token"], text=True).strip()
    request = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with opener(request, timeout=60) as response:
        return response.status


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="oauth_setup.py", description="Owner OAuth walkthrough.")
    parser.add_argument("--kind", choices=sorted(SCOPES), default="chat")
    parser.add_argument("--secret", default=None, help="target Secret Manager secret name")
    parser.add_argument("--client", default=None, help="path to the Desktop OAuth client JSON")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--email", default=os.environ.get("OWNER_EMAIL", ""), help="owner login hint")
    parser.add_argument("--verify-url", default=None, help="assistant /maintain URL to verify")
    parser.add_argument("--dry-run", action="store_true", help="print steps; run no consent/store")
    parser.add_argument("--print-scopes", action="store_true", help="print the scopes and exit")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.print_scopes:
        print(json.dumps(scopes_for(args.kind)))
        return 0

    print("# manual console steps:")
    for step in console_steps(args.kind):
        print("# - " + step)
    print("# scopes: " + ", ".join(scopes_for(args.kind)))

    if args.dry_run or not (args.client and args.secret and args.project):
        print("# dry-run / incomplete: no consent run and no secret written")
        return 0

    credentials = run_consent(args.client, scopes_for(args.kind), args.email or None)
    path = write_user_json(credentials)
    try:
        store_secret(args.secret, path, args.project)
        print("# stored secret version: " + args.secret + " (value not printed)")
    finally:
        os.remove(path)

    if args.verify_url:
        try:
            status = verify_maintain(args.verify_url)
            print("# /maintain HTTP " + str(status))
        except Exception as error:  # surface the type only, never the token
            print("# /maintain failed: " + type(error).__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

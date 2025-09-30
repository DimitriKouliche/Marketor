"""
Gmail Draft Generator for Game Key Distribution
Creates personalized Gmail drafts for "This is no cave" influencer outreach
Includes Steam keys from file
"""

import csv
import json
from datetime import datetime
from typing import List, Dict, Optional
import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import base64
from email.mime.text import MIMEText

GAME_NAME = "This is no cave"
STEAM_RELEASE_DATE = "October 17th"
YOUR_NAME = "Dimitri Kouliche"
YOUR_STUDIO = "monome.studio"
YOUR_EMAIL = "dimitri@monome.studio"

# Game Links
STEAM_PAGE = "https://store.steampowered.com/app/2852760/This_Is_No_Cave/"
PRESS_KIT = "https://drive.google.com/drive/folders/15G7kTkI2JRpEGLLCwWsPjb02QjdazZX2"
INSTAGRAM = "https://www.instagram.com/monome.studio/"
TIKTOK = "https://www.tiktok.com/@monomestudio"

# Gmail API Scopes
SCOPES = ['https://www.googleapis.com/auth/gmail.compose']


# ============= STEAM KEY MANAGEMENT =============

def load_steam_keys(key_file: str) -> List[str]:
    """
    Load Steam keys from file
    Supports common Steam key export formats:
    - One key per line
    - CSV with keys column
    """
    keys = []

    if not os.path.exists(key_file):
        print(f"❌ Key file not found: {key_file}")
        return keys

    # Try as plain text file first
    with open(key_file, 'r', encoding='utf-8') as f:
        first_line = f.readline().strip()
        f.seek(0)

        # Check if it's a CSV
        if ',' in first_line or '\t' in first_line:
            reader = csv.reader(f)
            header = next(reader, None)

            # Find key column (usually named "Key", "Steam Key", or similar)
            key_column = None
            if header:
                for i, col in enumerate(header):
                    if 'key' in col.lower():
                        key_column = i
                        break

            # If no header or key column found, assume first column
            if key_column is None:
                f.seek(0)
                key_column = 0

            for row in reader:
                if row and len(row) > key_column:
                    key = row[key_column].strip()
                    if key and len(key) > 10:  # Basic validation
                        keys.append(key)
        else:
            # Plain text file, one key per line
            for line in f:
                key = line.strip()
                if key and len(key) > 10:
                    keys.append(key)

    print(f"✓ Loaded {len(keys)} Steam keys from {key_file}")
    return keys


def save_key_assignment(assignments: Dict, filename: str = 'key_assignments.json'):
    """
    Save which key was assigned to which influencer
    """
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(assignments, f, indent=2)
    print(f"✓ Key assignments saved to {filename}")


def load_key_assignments(filename: str = 'key_assignments.json') -> Dict:
    """
    Load existing key assignments to avoid duplicates
    """
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


# ============= EMAIL TEMPLATES =============

def generate_email_content(influencer: Dict, steam_key: str) -> Dict[str, str]:
    """
    Generate personalized email content for "This is no cave"
    """
    name = influencer.get('display_name') or influencer.get('username', 'there')
    platform = influencer.get('platform', 'YouTube')
    followers = influencer.get('followers', 0)
    last_video = influencer.get('last_video_title', 'recent content')
    last_game = influencer.get('last_game_played', '')

    # Format follower count
    if followers >= 1000000:
        follower_str = f"{followers / 1000000:.1f}M"
    elif followers >= 1000:
        follower_str = f"{followers / 1000:.1f}K"
    else:
        follower_str = str(followers)

    # Personalization based on their content
    opening_line = ""
    if last_game and any(speedrun_game in last_game.lower() for speedrun_game in
                         ['celeste', 'meat boy', 'hollow knight', 'cuphead', 'ori']):
        opening_line = f"I saw you recently played {last_game} - clearly you appreciate tight, skill-based platformers!"
    elif last_video and 'speedrun' in last_video.lower():
        opening_line = f"I loved your speedrun content in \"{last_video[:45]}{'...' if len(last_video) > 45 else ''}\" - you're going to love this game!"
    elif last_game:
        opening_line = f"I saw you recently played {last_game}, and thought you might enjoy something a bit different!"
    elif last_video:
        opening_line = f"I loved your recent video \"{last_video[:50]}{'...' if len(last_video) > 50 else ''}\" - your {follower_str} community clearly appreciates great gaming content!"
    else:
        opening_line = f"Your {follower_str} community clearly appreciates great gaming content!"

    # Check if they're indie-positive or speedrun-focused
    sentiment = influencer.get('indie_sentiment', 'neutral')
    indie_positive = sentiment in ['very_positive', 'positive']

    # Check platform for social media angle
    is_tiktok_instagram = platform.lower() in ['instagram', 'tiktok']

    # Determine which unique features to emphasize
    if 'speedrun' in last_video.lower() or 'speed' in last_video.lower():
        feature_hook = "The game was designed with speedrunners in mind - every level has leaderboards and the movement system rewards mastery. Plus, it's mouse-controlled, which adds a unique skill ceiling!"
    elif any(coop in last_video.lower() for coop in ['co-op', 'coop', 'multiplayer', 'local', '4 player']):
        feature_hook = "The 4-player local co-op is perfect for collaborative content - the chaos of coordinating mouse movements with friends is hilarious and challenging!"
    elif is_tiktok_instagram:
        feature_hook = "The art style and animations have been killing it on social media (check our Instagram/TikTok if you're curious!) - very satisfying movement and visual feedback."
    else:
        feature_hook = "It's got that 'one more try' addictiveness that makes for great content - tight controls, leaderboards, and surprising depth despite the simple mouse controls."

    # Email subject - personalized and intriguing
    if 'speedrun' in last_video.lower():
        subject = f"Speedrunner's dream? {GAME_NAME} key for you"
    elif any(coop in last_video.lower() for coop in ['co-op', 'coop', 'multiplayer']):
        subject = f"4-player chaos: {GAME_NAME} key inside"
    else:
        subject = f"Steam key: {GAME_NAME} (mouse-controlled platformer)"

    # Email body
    body = f"""Hi {name},

{opening_line}

I'm {YOUR_NAME} from {YOUR_STUDIO}, and I've been developing {GAME_NAME} - a fast-paced 2D platformer that's fully playable with just a mouse (gamepad support too!). It launches on Steam on {STEAM_RELEASE_DATE}.

{feature_hook}

Here's what makes it special:
• Mouse-only controls (surprisingly challenging and satisfying!)
• Built for speedrunning with leaderboards on every level
• 4-player local co-op (perfect for couch gaming content!)
• Infinite roguelite mode with procedurally generated levels
• Eye-catching art style that performs great on social media

I'd love for you to check it out before launch. Here's your personal Steam key:

🔑 {steam_key}

No pressure whatsoever - if you enjoy it and want to share it with your audience, that would be incredible. If it's not your thing, no worries at all! I genuinely appreciate any feedback either way.

Want to see it in action first?
Steam Page: {STEAM_PAGE}
Press Kit (trailers, screenshots, GIFs): {PRESS_KIT}

Our socials if you want a preview of the visual style:
Instagram: {INSTAGRAM}
TikTok: {TIKTOK}

Happy to answer any questions or provide additional info/assets!

Best,
{YOUR_NAME}
{YOUR_STUDIO}

P.S. - If you're into speedrunning, I'd be really curious to see what times you can get on the leaderboards. The movement tech has some surprising depth once you master it!"""

    return {
        "subject": subject,
        "body": body,
        "to": influencer.get('primary_email', influencer.get('emails', '').split(',')[0].strip())
    }


def generate_followup_email(influencer: Dict, original_key: str) -> Dict[str, str]:
    """
    Generate a gentle follow-up email (for non-responders after 7-10 days)
    """
    name = influencer.get('display_name') or influencer.get('username', 'there')

    subject = f"Quick follow-up: {GAME_NAME} launches tomorrow!"

    body = f"""Hi {name},

Just a quick follow-up on the {GAME_NAME} Steam key I sent last week. The game launches tomorrow ({STEAM_RELEASE_DATE}), and I wanted to make sure the key worked for you!

Your key again: {original_key}

If you've had a chance to try it, I'd love to hear what you think - especially curious if you've climbed any of the leaderboards! 

If you're not interested or don't have time, totally understand - just let me know and I won't bother you again. 😊

Either way, thanks for your time!

{YOUR_NAME}
{YOUR_STUDIO}

Steam Page: {STEAM_PAGE}"""

    return {
        "subject": subject,
        "body": body,
        "to": influencer.get('primary_email', influencer.get('emails', '').split(',')[0].strip())
    }


# ============= GMAIL API FUNCTIONS =============

def get_gmail_service():
    """
    Authenticate and return Gmail API service
    """
    creds = None

    # Token file stores user's access and refresh tokens
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    # If no valid credentials, let user log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists('credentials.json'):
                print("\n❌ ERROR: credentials.json not found!")
                print("\nTo use Gmail API, you need to:")
                print("1. Go to https://console.cloud.google.com/")
                print("2. Create a new project (or select existing)")
                print("3. Enable Gmail API")
                print("4. Create OAuth 2.0 credentials (Desktop app)")
                print("5. Download credentials.json to this directory")
                print("\nDetailed guide: https://developers.google.com/gmail/api/quickstart/python\n")
                return None

            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)

        # Save credentials for next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    try:
        service = build('gmail', 'v1', credentials=creds)
        return service
    except HttpError as error:
        print(f'❌ An error occurred: {error}')
        return None


def create_draft(service, email_content: Dict, influencer_name: str) -> bool:
    """
    Create a Gmail draft
    """
    try:
        message = MIMEText(email_content['body'])
        message['to'] = email_content['to']
        message['subject'] = email_content['subject']

        # Encode message
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

        create_message = {
            'message': {
                'raw': encoded_message
            }
        }

        draft = service.users().drafts().create(userId='me', body=create_message).execute()

        print(f"  ✓ Draft created for {influencer_name}")
        return True

    except HttpError as error:
        print(f'  ❌ Error creating draft for {influencer_name}: {error}')
        return False


# ============= MAIN CAMPAIGN GENERATOR =============

def generate_campaign(
        csv_file: str,
        key_file: str,
        max_drafts: int = None,
        create_gmail_drafts: bool = True
):
    """
    Generate personalized email campaign with Steam keys
    """
    print("=" * 70)
    print(f"STEAM KEY DISTRIBUTION CAMPAIGN: {GAME_NAME}")
    print("=" * 70)

    # Load influencers
    print(f"\n[1/5] Loading influencers from {csv_file}...")
    influencers = []
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        influencers = list(reader)

    print(f"  ✓ Loaded {len(influencers)} influencers")

    # Filter valid emails
    valid_influencers = []
    for inf in influencers:
        emails = inf.get('emails', 'Not found')
        if emails and emails != 'Not found' and '@' in emails:
            email = emails.split(',')[0].strip()
            inf['primary_email'] = email
            valid_influencers.append(inf)

    print(f"  ✓ {len(valid_influencers)} have valid email addresses")

    # Load Steam keys
    print(f"\n[2/5] Loading Steam keys from {key_file}...")
    steam_keys = load_steam_keys(key_file)

    if len(steam_keys) == 0:
        print("❌ No Steam keys found. Aborting.")
        return

    # Load existing assignments
    key_assignments = load_key_assignments()

    # Check if we have enough keys
    keys_needed = min(len(valid_influencers), max_drafts) if max_drafts else len(valid_influencers)
    keys_available = len(steam_keys) - len(key_assignments)

    if keys_available < keys_needed:
        print(f"\n⚠️  WARNING: Only {keys_available} keys available, but {keys_needed} needed")
        print(f"  Already assigned: {len(key_assignments)} keys")
        keys_needed = keys_available

    print(f"  ✓ Will create {keys_needed} drafts")

    # Limit influencers if needed
    if max_drafts and max_drafts < len(valid_influencers):
        valid_influencers = valid_influencers[:max_drafts]

    # Connect to Gmail if creating drafts
    gmail_service = None
    if create_gmail_drafts:
        print(f"\n[3/5] Connecting to Gmail API...")
        gmail_service = get_gmail_service()
        if not gmail_service:
            print("\n⚠️  Gmail API not available. Will generate email files instead.")
            create_gmail_drafts = False

    # Generate emails and assign keys
    print(f"\n[4/5] Generating personalized emails...")

    drafts_created = 0
    emails_generated = []
    key_index = len(key_assignments)  # Start from next available key

    for i, influencer in enumerate(valid_influencers[:keys_needed], 1):
        email_addr = influencer['primary_email']

        # Skip if already assigned
        if email_addr in key_assignments:
            print(f"  ⏭️  Skipping {influencer.get('username')} - already assigned key")
            continue

        # Assign next available key
        steam_key = steam_keys[key_index]
        key_index += 1

        # Generate email
        email_content = generate_email_content(influencer, steam_key)

        # Save assignment
        key_assignments[email_addr] = {
            "key": steam_key,
            "influencer": influencer.get('username'),
            "platform": influencer.get('platform'),
            "followers": influencer.get('followers'),
            "assigned_date": datetime.now().isoformat(),
            "sent": False
        }

        print(f"  [{i}/{keys_needed}] {influencer.get('username')} ({email_addr})")

        # Create Gmail draft or save to file
        if create_gmail_drafts and gmail_service:
            success = create_draft(gmail_service, email_content, influencer.get('username'))
            if success:
                drafts_created += 1
                key_assignments[email_addr]["draft_created"] = True
        else:
            # Save email to file for manual sending
            emails_generated.append({
                "influencer": influencer.get('username'),
                "email": email_addr,
                "subject": email_content['subject'],
                "body": email_content['body'],
                "steam_key": steam_key
            })

    # Save key assignments
    print(f"\n[5/5] Saving key assignments...")
    save_key_assignment(key_assignments)

    # Save emails to file if not using Gmail API
    if not create_gmail_drafts and emails_generated:
        output_file = 'email_drafts.txt'
        with open(output_file, 'w', encoding='utf-8') as f:
            for email_data in emails_generated:
                f.write("=" * 70 + "\n")
                f.write(f"TO: {email_data['email']}\n")
                f.write(f"INFLUENCER: {email_data['influencer']}\n")
                f.write(f"STEAM KEY: {email_data['steam_key']}\n")
                f.write("-" * 70 + "\n")
                f.write(f"SUBJECT: {email_data['subject']}\n")
                f.write("-" * 70 + "\n")
                f.write(f"{email_data['body']}\n")
                f.write("\n\n")

        print(f"  ✓ Email drafts saved to {output_file}")

    # Summary
    print("\n" + "=" * 70)
    print("CAMPAIGN SUMMARY")
    print("=" * 70)

    if create_gmail_drafts:
        print(f"Gmail drafts created: {drafts_created}")
        print(f"\n✓ Check your Gmail drafts folder!")
        print(f"✓ Review each email before sending")
        print(f"✓ You can edit any draft before sending")
    else:
        print(f"Email drafts generated: {len(emails_generated)}")
        print(f"✓ Drafts saved to: email_drafts.txt")
        print(f"✓ Copy/paste into your email client")

    print(f"\nSteam keys assigned: {len([k for k in key_assignments.values() if not k.get('sent', False)])}")
    print(f"Keys remaining: {len(steam_keys) - len(key_assignments)}")
    print(f"\n📊 Track assignments in: key_assignments.json")

    return key_assignments


def mark_as_sent(email_addresses: List[str]):
    """
    Mark emails as sent after you send them manually
    Call this with list of email addresses you've sent to
    """
    assignments = load_key_assignments()

    for email in email_addresses:
        if email in assignments:
            assignments[email]['sent'] = True
            assignments[email]['sent_date'] = datetime.now().isoformat()

    save_key_assignment(assignments)
    print(f"✓ Marked {len(email_addresses)} emails as sent")


def generate_followups(days_since: int = 7):
    """
    Generate follow-up drafts for non-responders
    """
    print("=" * 70)
    print("GENERATING FOLLOW-UP EMAILS")
    print("=" * 70)

    assignments = load_key_assignments()

    followup_list = []
    for email, data in assignments.items():
        if data.get('sent', False):
            sent_date = datetime.fromisoformat(data['sent_date'])
            days_passed = (datetime.now() - sent_date).days

            # Check if needs follow-up
            if days_passed >= days_since and not data.get('responded', False):
                followup_list.append({
                    "email": email,
                    "influencer": data['influencer'],
                    "key": data['key']
                })

    print(f"\nFound {len(followup_list)} influencers needing follow-up")

    # Generate follow-up drafts
    # Implementation similar to main campaign

    return followup_list


# ============= MAIN EXECUTION =============

def main():
    """
    Main execution
    """
    import argparse

    parser = argparse.ArgumentParser(
        description='Generate personalized Gmail drafts for game key distribution'
    )
    parser.add_argument(
        '--csv',
        type=str,
        default='influencers_priority_top50.csv',
        help='CSV file with influencer data'
    )
    parser.add_argument(
        '--keys',
        type=str,
        default='steam_keys.txt',
        help='File containing Steam keys (one per line or CSV)'
    )
    parser.add_argument(
        '--max',
        type=int,
        default=None,
        help='Maximum number of drafts to create'
    )
    parser.add_argument(
        '--no-gmail',
        action='store_true',
        help='Generate text file instead of Gmail drafts'
    )
    parser.add_argument(
        '--followup',
        action='store_true',
        help='Generate follow-up emails for non-responders'
    )

    args = parser.parse_args()

    if args.followup:
        generate_followups()
    else:
        generate_campaign(
            csv_file=args.csv,
            key_file=args.keys,
            max_drafts=args.max,
            create_gmail_drafts=not args.no_gmail
        )


if __name__ == "__main__":
    main()
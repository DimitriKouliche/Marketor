"""
Influencer Discovery & Contact Extraction Automation
Finds gaming influencers and extracts contact info for outreach
"""

import requests
from datetime import datetime, timedelta
import json
import re
import csv
from typing import List, Dict, Optional
import time

# ============= CONFIGURATION =============
YOUTUBE_API_KEY = "YOUR_YOUTUBE_API_KEY"
TWITCH_CLIENT_ID = "YOUR_TWITCH_CLIENT_ID"
TWITCH_CLIENT_SECRET = "YOUR_TWITCH_CLIENT_SECRET"

MIN_FOLLOWERS = 500
MAX_FOLLOWERS = 100000
DAYS_SINCE_LAST_VIDEO = 60

CHANNEL_CACHE = {}

# ============= EMAIL EXTRACTION =============

def extract_emails(text: str) -> List[str]:
    """Extract email addresses from text using regex"""
    if not text:
        return []

    text = text.replace('[at]', '@').replace('(at)', '@').replace(' at ', '@')
    text = text.replace('[dot]', '.').replace('(dot)', '.').replace(' dot ', '.')
    text = text.replace('[AT]', '@').replace('(AT)', '@')
    text = text.replace('[DOT]', '.').replace('(DOT)', '.')
    text = text.replace(' @ ', '@').replace(' . ', '.')

    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    emails = re.findall(email_pattern, text)

    filtered_emails = []
    for email in emails:
        email_lower = email.lower()
        if not any(skip in email_lower for skip in ['example.com', 'domain.com', 'email.com', 'youremail', 'noreply', 'support@']):
            filtered_emails.append(email)

    return list(set(filtered_emails))


def extract_social_links(text: str) -> Dict[str, str]:
    """Extract social media links from text"""
    if not text:
        return {}

    social_links = {}
    patterns = {
        'twitter': r'twitter\.com/([A-Za-z0-9_]+)',
        'instagram': r'instagram\.com/([A-Za-z0-9_.]+)',
        'discord': r'discord\.gg/([A-Za-z0-9]+)',
        'tiktok': r'tiktok\.com/@([A-Za-z0-9_.]+)',
    }

    for platform, pattern in patterns.items():
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            social_links[platform] = matches[0]

    return social_links


def extract_business_terms(text: str) -> List[str]:
    """Extract business/sponsorship related keywords from bio"""
    if not text:
        return []

    keywords = [
        'business inquiries', 'business email', 'sponsorships',
        'partnerships', 'collaborations', 'contact', 'booking',
        'brand deals', 'marketing', 'pr', 'management'
    ]

    found_terms = []
    text_lower = text.lower()

    for keyword in keywords:
        if keyword in text_lower:
            found_terms.append(keyword)

    return found_terms


def is_gaming_channel(description: str, video_titles: List[str]) -> bool:
    """Check if channel is a gaming content creator (not a game developer)"""
    text = (description + " " + " ".join(video_titles)).lower()

    dev_keywords = [
        'game developer', 'game dev', 'gamedev', 'indie dev',
        'developing', 'my game', 'our game', 'game studio',
        'game designer', 'game artist', 'game programmer',
        'unity tutorial', 'unreal tutorial', 'godot tutorial',
        'game development', 'making games', 'created by'
    ]

    creator_keywords = [
        'gameplay', 'playthrough', 'lets play', "let's play",
        'walkthrough', 'speedrun', 'playing', 'streamer',
        'twitch', 'gamer', 'gaming channel', 'game review',
        'first impressions', 'indie game showcase'
    ]

    non_gaming = [
        'vlog', 'recipe', 'cooking', 'makeup', 'fashion',
        'lifestyle', 'music video', 'official music'
    ]

    dev_score = sum(2 for keyword in dev_keywords if keyword in text)
    creator_score = sum(1 for keyword in creator_keywords if keyword in text)
    non_gaming_score = sum(1 for keyword in non_gaming if keyword in text)

    if dev_score > 1:
        return False

    if non_gaming_score > creator_score:
        return False

    return creator_score >= 2


# ============= YOUTUBE FUNCTIONS =============

def search_youtube_platformer_videos(api_key: str, days: int = 30) -> List[Dict]:
    """Search YouTube for recent platformer gaming videos (optimized)"""
    base_url = "https://www.googleapis.com/youtube/v3/search"
    published_after = (datetime.now() - timedelta(days=days)).isoformat() + "Z"

    # Reduced to 8 high-quality keywords
    keywords = [
        "celeste gameplay",
        "hollow knight gameplay",
        "indie platformer gameplay",
        "metroidvania gameplay",
        "platformer speedrun",
        "2d platformer let's play",
        "cuphead gameplay",
        "dead cells gameplay"
    ]

    all_channels = []
    seen_video_ids = set()

    for keyword in keywords:
        params = {
            "part": "snippet",
            "q": keyword,
            "type": "video",
            "videoCategoryId": "20",
            "publishedAfter": published_after,
            "maxResults": 50,  # Keep at 50
            "key": api_key
        }

        try:
            response = requests.get(base_url, params=params)

            if response.status_code == 200:
                data = response.json()

                for item in data.get("items", []):
                    video_id = item["id"]["videoId"]

                    # Skip duplicate videos
                    if video_id in seen_video_ids:
                        continue

                    seen_video_ids.add(video_id)

                    all_channels.append(
                        {
                            "channel_id": item["snippet"]["channelId"],
                            "channel_title": item["snippet"]["channelTitle"],
                            "video_title": item["snippet"]["title"],
                            "video_id": video_id,
                            "published_at": item["snippet"]["publishedAt"],
                            "platform": "youtube"
                        }
                    )

                time.sleep(0.1)
            elif response.status_code == 403:
                print(f"  ⚠️ YouTube API quota exceeded")
                break

        except Exception as e:
            print(f"  ⚠️ Error searching '{keyword}': {e}")
            continue

    print(f"  ➤ Found {len(all_channels)} total videos from searches")
    return all_channels


def get_youtube_channel_details(api_key: str, channel_id: str) -> Optional[Dict]:
    """Get channel information (with caching)"""

    # Check cache first
    if channel_id in CHANNEL_CACHE:
        return CHANNEL_CACHE[channel_id]

    base_url = "https://www.googleapis.com/youtube/v3/channels"

    params = {
        "part": "statistics,snippet",  # Removed brandingSettings to save quota
        "id": channel_id,
        "key": api_key
    }

    try:
        response = requests.get(base_url, params=params)

        if response.status_code == 200:
            data = response.json()

            if data.get("items"):
                item = data["items"][0]
                snippet = item["snippet"]
                stats = item["statistics"]

                result = {
                    "channel_id": channel_id,
                    "title": snippet["title"],
                    "custom_url": snippet.get("customUrl", ""),
                    "description": snippet.get("description", ""),
                    "subscriber_count": int(stats.get("subscriberCount", 0)),
                    "view_count": int(stats.get("viewCount", 0)),
                    "video_count": int(stats.get("videoCount", 0)),
                    "country": snippet.get("country", ""),
                    "created_at": snippet.get("publishedAt", ""),
                    "url": f"https://youtube.com/channel/{channel_id}"
                }

                # Cache the result
                CHANNEL_CACHE[channel_id] = result
                return result
    except Exception as e:
        return None

    return None


def get_youtube_recent_videos(api_key: str, channel_id: str, max_results: int = 10) -> List[Dict]:
    """Get recent videos from a channel"""
    base_url = "https://www.googleapis.com/youtube/v3/search"

    params = {
        "part": "snippet",
        "channelId": channel_id,
        "order": "date",
        "type": "video",
        "maxResults": max_results,
        "key": api_key
    }

    try:
        response = requests.get(base_url, params=params)
        videos = []

        if response.status_code == 200:
            data = response.json()
            video_ids = [item["id"]["videoId"] for item in data.get("items", [])]

            if video_ids:
                stats_url = "https://www.googleapis.com/youtube/v3/videos"
                stats_params = {
                    "part": "statistics,snippet",
                    "id": ",".join(video_ids),
                    "key": api_key
                }

                stats_response = requests.get(stats_url, params=stats_params)

                if stats_response.status_code == 200:
                    stats_data = stats_response.json()

                    for video in stats_data.get("items", []):
                        videos.append({
                            "video_id": video["id"],
                            "title": video["snippet"]["title"],
                            "published_at": video["snippet"]["publishedAt"],
                            "view_count": int(video["statistics"].get("viewCount", 0)),
                            "like_count": int(video["statistics"].get("likeCount", 0)),
                            "comment_count": int(video["statistics"].get("commentCount", 0))
                        })
    except Exception as e:
        pass

    return videos


def calculate_youtube_metrics(videos: List[Dict], channel_created: str) -> Dict:
    """Calculate upload frequency and average views from recent videos"""
    if not videos:
        return {
            "upload_frequency_days": 0,
            "avg_views_per_video": 0,
            "avg_likes_per_video": 0,
            "upload_consistency": "unknown"
        }

    avg_views = sum(v["view_count"] for v in videos) / len(videos)
    avg_likes = sum(v["like_count"] for v in videos) / len(videos)

    if len(videos) >= 2:
        dates = [datetime.fromisoformat(v["published_at"].replace("Z", "+00:00")) for v in videos]
        dates.sort(reverse=True)

        time_diffs = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
        avg_frequency = sum(time_diffs) / len(time_diffs)

        variance = sum((d - avg_frequency) ** 2 for d in time_diffs) / len(time_diffs)
        std_dev = variance ** 0.5

        if std_dev < 3:
            consistency = "very_consistent"
        elif std_dev < 7:
            consistency = "consistent"
        elif std_dev < 14:
            consistency = "somewhat_consistent"
        else:
            consistency = "inconsistent"
    else:
        avg_frequency = 0
        consistency = "unknown"

    return {
        "upload_frequency_days": round(avg_frequency, 1) if len(videos) >= 2 else 0,
        "avg_views_per_video": int(avg_views),
        "avg_likes_per_video": int(avg_likes),
        "upload_consistency": consistency
    }


def process_youtube_channels(api_key: str, channels: List[Dict], min_subs: int, max_subs: int) -> List[Dict]:
    """Process YouTube channels (optimized with batch filtering)"""
    processed = []
    seen_channels = set()

    # Group by channel to reduce API calls
    channels_by_id = {}
    for channel_data in channels:
        channel_id = channel_data["channel_id"]
        if channel_id not in channels_by_id:
            channels_by_id[channel_id] = channel_data

    print(f"  ➤ Processing {len(channels_by_id)} unique channels...")

    for channel_id, channel_data in channels_by_id.items():
        details = get_youtube_channel_details(api_key, channel_id)

        if not details:
            continue

        # Quick filter by subscriber count first (before expensive API calls)
        if not (min_subs <= details["subscriber_count"] <= max_subs):
            continue

        description = details["description"]

        # Only get recent videos if we passed initial filters
        recent_videos = get_youtube_recent_videos(api_key, channel_id, max_results=5)  # Reduced from 10
        video_titles = [v.get("title", "") for v in recent_videos]

        if not is_gaming_channel(description, video_titles):
            continue

        emails = extract_emails(description)
        social_links = extract_social_links(description)
        business_terms = extract_business_terms(description)
        sentiment_analysis = analyze_sentiment(description)
        metrics = calculate_youtube_metrics(recent_videos, details.get("created_at", ""))

        engagement_rate_str = calculate_engagement_rate(
            details["view_count"], details["subscriber_count"],
            details["video_count"]
        )

        if metrics["avg_views_per_video"] > 0 and details["subscriber_count"] > 0:
            engagement_numeric = (metrics["avg_views_per_video"] / details["subscriber_count"]) * 100
        else:
            engagement_numeric = 0

        influencer_data = {
            "platform": "YouTube",
            "username": details["title"],
            "custom_url": details["custom_url"],
            "url": details["url"],
            "followers": details["subscriber_count"],
            "total_views": details["view_count"],
            "video_count": details["video_count"],
            "country": details["country"],
            "last_video_title": channel_data["video_title"],
            "last_video_date": channel_data["published_at"],
            "last_video_url": f"https://youtube.com/watch?v={channel_data['video_id']}",
            "emails": ", ".join(emails) if emails else "Not found",
            "email_count": len(emails),
            "twitter": social_links.get("twitter", ""),
            "instagram": social_links.get("instagram", ""),
            "discord": social_links.get("discord", ""),
            "tiktok": social_links.get("tiktok", ""),
            "has_business_terms": "Yes" if business_terms else "No",
            "business_terms": ", ".join(business_terms),
            "bio_snippet": description[:200] + "..." if len(description) > 200 else description,
            "engagement_rate": engagement_rate_str,
            "engagement_rate_numeric": round(engagement_numeric, 2),
            "avg_views_per_video": metrics["avg_views_per_video"],
            "avg_likes_per_video": metrics["avg_likes_per_video"],
            "upload_frequency_days": metrics["upload_frequency_days"],
            "upload_consistency": metrics["upload_consistency"],
            "indie_sentiment": sentiment_analysis["sentiment"],
            "indie_sentiment_score": sentiment_analysis["score"],
            "indie_sentiment_indicators": ", ".join(sentiment_analysis["indicators"])
        }

        response_analysis = calculate_response_likelihood(influencer_data)
        influencer_data["response_likelihood"] = response_analysis["likelihood"]
        influencer_data["response_score"] = response_analysis["score"]
        influencer_data["response_factors"] = " | ".join(response_analysis["factors"])

        influencer_data["icebreaker"] = generate_icebreaker(
            platform="YouTube",
            name=details["title"],
            recent_video=channel_data["video_title"],
            subscriber_count=details["subscriber_count"]
        )

        processed.append(influencer_data)

    return processed


# ============= TWITCH FUNCTIONS =============

def get_twitch_oauth_token(client_id: str, client_secret: str) -> Optional[str]:
    """Get OAuth token for Twitch API"""
    url = "https://id.twitch.tv/oauth2/token"

    params = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials"
    }

    response = requests.post(url, params=params)

    if response.status_code == 200:
        return response.json()["access_token"]

    return None


def search_twitch_game_id(access_token: str, client_id: str, game_name: str) -> Optional[str]:
    """Get Twitch game ID by name"""
    url = "https://api.twitch.tv/helix/games"

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}"
    }

    params = {"name": game_name}

    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        data = response.json()
        if data.get("data"):
            return data["data"][0]["id"]

    return None


def get_twitch_streamers_by_game(access_token: str, client_id: str, game_id: str, days: int = 60) -> List[Dict]:
    """Get streamers who played a specific game recently"""
    url = "https://api.twitch.tv/helix/videos"

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}"
    }

    params = {
        "game_id": game_id,
        "first": 100,  # Changed from 300 to 100 (max allowed)
        "type": "archive",
        "sort": "time",  # Add this
        "period": "month"  # Add this - gets videos from last month
    }

    response = requests.get(url, headers=headers, params=params)

    streamers = []

    if response.status_code == 200:
        data = response.json()

        print(f"     API returned {len(data.get('data', []))} total videos")  # Debug line

        for video in data.get("data", []):
            created_at = datetime.fromisoformat(video["created_at"].replace("Z", "+00:00"))
            days_old = (datetime.now(created_at.tzinfo) - created_at).days

            if days_old <= days:
                streamers.append(
                    {
                        "user_id": video["user_id"],
                        "user_name": video["user_name"],
                        "video_title": video["title"],
                        "video_url": video["url"],
                        "created_at": video["created_at"],
                        "view_count": video["view_count"],
                        "game_name": video.get("game_name", ""),
                        "platform": "twitch"
                    }
                )
    else:
        print(f"     Twitch API error: {response.status_code}")
        print(f"     Response: {response.text[:200]}")  # Debug line

    return streamers


def get_twitch_user_details(access_token: str, client_id: str, user_id: str) -> Optional[Dict]:
    """Get detailed user information"""
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}"
    }

    user_url = "https://api.twitch.tv/helix/users"
    user_params = {"id": user_id}
    user_response = requests.get(user_url, headers=headers, params=user_params)

    if user_response.status_code != 200:
        return None

    user_data = user_response.json().get("data", [])
    if not user_data:
        return None

    user = user_data[0]

    follower_url = "https://api.twitch.tv/helix/channels/followers"
    follower_params = {"broadcaster_id": user_id}
    follower_response = requests.get(follower_url, headers=headers, params=follower_params)

    follower_count = 0
    if follower_response.status_code == 200:
        follower_count = follower_response.json().get("total", 0)

    return {
        "user_id": user_id,
        "username": user["login"],
        "display_name": user["display_name"],
        "description": user.get("description", ""),
        "follower_count": follower_count,
        "view_count": user.get("view_count", 0),
        "broadcaster_type": user.get("broadcaster_type", ""),
        "created_at": user.get("created_at", ""),
        "url": f"https://twitch.tv/{user['login']}"
    }


def get_twitch_user_videos(access_token: str, client_id: str, user_id: str, max_results: int = 10) -> List[Dict]:
    """Get recent videos/VODs from a Twitch user"""
    url = "https://api.twitch.tv/helix/videos"

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}"
    }

    params = {
        "user_id": user_id,
        "first": max_results,
        "type": "archive"
    }

    response = requests.get(url, headers=headers, params=params)
    videos = []

    if response.status_code == 200:
        data = response.json()

        for video in data.get("data", []):
            videos.append({
                "video_id": video["id"],
                "published_at": video["created_at"],
                "view_count": video["view_count"],
                "duration": video["duration"]
            })

    return videos


def calculate_twitch_metrics(videos: List[Dict]) -> Dict:
    """Calculate upload frequency and average views for Twitch"""
    if not videos:
        return {
            "upload_frequency_days": 0,
            "avg_views_per_video": 0,
            "upload_consistency": "unknown"
        }

    avg_views = sum(v["view_count"] for v in videos) / len(videos)

    if len(videos) >= 2:
        dates = [datetime.fromisoformat(v["published_at"].replace("Z", "+00:00")) for v in videos]
        dates.sort(reverse=True)

        time_diffs = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
        avg_frequency = sum(time_diffs) / len(time_diffs)

        variance = sum((d - avg_frequency) ** 2 for d in time_diffs) / len(time_diffs)
        std_dev = variance ** 0.5

        if std_dev < 3:
            consistency = "very_consistent"
        elif std_dev < 7:
            consistency = "consistent"
        elif std_dev < 14:
            consistency = "somewhat_consistent"
        else:
            consistency = "inconsistent"
    else:
        avg_frequency = 0
        consistency = "unknown"

    return {
        "upload_frequency_days": round(avg_frequency, 1) if len(videos) >= 2 else 0,
        "avg_views_per_video": int(avg_views),
        "upload_consistency": consistency
    }


def process_twitch_streamers(access_token: str, client_id: str, streamers: List[Dict], min_followers: int,
                             max_followers: int) -> List[Dict]:
    """Process Twitch streamers with contact extraction"""
    processed = []
    seen_users = set()

    for streamer in streamers:
        user_id = streamer["user_id"]

        if user_id in seen_users:
            continue

        seen_users.add(user_id)

        details = get_twitch_user_details(access_token, client_id, user_id)

        if details and min_followers <= details["follower_count"] <= max_followers:
            description = details["description"]
            emails = extract_emails(description)
            social_links = extract_social_links(description)
            business_terms = extract_business_terms(description)
            sentiment_analysis = analyze_sentiment(description)
            recent_videos = get_twitch_user_videos(access_token, client_id, user_id, max_results=10)
            metrics = calculate_twitch_metrics(recent_videos)

            engagement_numeric = 0
            engagement_str = "N/A"
            if metrics["avg_views_per_video"] > 0 and details["follower_count"] > 0:
                engagement_numeric = (metrics["avg_views_per_video"] / details["follower_count"]) * 100
                engagement_str = f"{engagement_numeric:.2f}%"

            influencer_data = {
                "platform": "Twitch",
                "username": details["username"],
                "display_name": details["display_name"],
                "url": details["url"],
                "followers": details["follower_count"],
                "total_views": details["view_count"],
                "broadcaster_type": details["broadcaster_type"],
                "last_video_title": streamer["video_title"],
                "last_video_date": streamer["created_at"],
                "last_video_url": streamer["video_url"],
                "last_game_played": streamer.get("game_name", ""),
                "video_count": len(recent_videos),
                "country": "",
                "emails": ", ".join(emails) if emails else "Not found",
                "email_count": len(emails),
                "twitter": social_links.get("twitter", ""),
                "instagram": social_links.get("instagram", ""),
                "discord": social_links.get("discord", ""),
                "tiktok": social_links.get("tiktok", ""),
                "has_business_terms": "Yes" if business_terms else "No",
                "business_terms": ", ".join(business_terms),
                "bio_snippet": description[:200] + "..." if len(description) > 200 else description,
                "engagement_rate": engagement_str,
                "engagement_rate_numeric": round(engagement_numeric, 2),
                "avg_views_per_video": metrics["avg_views_per_video"],
                "avg_likes_per_video": 0,
                "upload_frequency_days": metrics["upload_frequency_days"],
                "upload_consistency": metrics["upload_consistency"],
                "indie_sentiment": sentiment_analysis["sentiment"],
                "indie_sentiment_score": sentiment_analysis["score"],
                "indie_sentiment_indicators": ", ".join(sentiment_analysis["indicators"])
            }

            response_analysis = calculate_response_likelihood(influencer_data)
            influencer_data["response_likelihood"] = response_analysis["likelihood"]
            influencer_data["response_score"] = response_analysis["score"]
            influencer_data["response_factors"] = " | ".join(response_analysis["factors"])

            influencer_data["icebreaker"] = generate_icebreaker(
                platform="Twitch",
                name=details["display_name"],
                recent_video=streamer["video_title"],
                subscriber_count=details["follower_count"],
                game_name=streamer.get("game_name", "")
            )

            processed.append(influencer_data)

    return processed


# ============= HELPER FUNCTIONS =============

def calculate_engagement_rate(total_views: int, subscribers: int, video_count: int) -> str:
    """Calculate approximate engagement rate"""
    if subscribers == 0 or video_count == 0:
        return "N/A"

    avg_views_per_video = total_views / video_count
    engagement = (avg_views_per_video / subscribers) * 100

    return f"{engagement:.2f}%"


def analyze_sentiment(text: str) -> Dict[str, any]:
    """Analyze sentiment toward indie games in bio/description"""
    if not text:
        return {"score": 0, "sentiment": "neutral", "indicators": []}

    text_lower = text.lower()

    positive_keywords = [
        'indie', 'independent games', 'small studios', 'indie dev',
        'support indie', 'hidden gems', 'indie titles', 'indie scene',
        'unique games', 'creative games', 'innovative', 'experimental',
        'passion project', 'handcrafted', 'artistic', 'indie darling'
    ]

    negative_keywords = [
        'aaa only', 'no indie', 'major titles only', 'big budget only',
        'triple-a exclusive'
    ]

    neutral_keywords = [
        'all games', 'variety', 'mixed content', 'different genres'
    ]

    positive_count = sum(1 for keyword in positive_keywords if keyword in text_lower)
    negative_count = sum(1 for keyword in negative_keywords if keyword in text_lower)
    neutral_count = sum(1 for keyword in neutral_keywords if keyword in text_lower)

    score = (positive_count * 2) - (negative_count * 3) + (neutral_count * 0.5)
    score = max(-10, min(10, score))

    if score >= 3:
        sentiment = "very_positive"
    elif score >= 1:
        sentiment = "positive"
    elif score >= -1:
        sentiment = "neutral"
    elif score >= -3:
        sentiment = "negative"
    else:
        sentiment = "very_negative"

    indicators = []
    for keyword in positive_keywords:
        if keyword in text_lower:
            indicators.append(f"+{keyword}")
    for keyword in negative_keywords:
        if keyword in text_lower:
            indicators.append(f"-{keyword}")

    return {
        "score": round(score, 2),
        "sentiment": sentiment,
        "indicators": indicators[:5]
    }


def calculate_response_likelihood(influencer_data: Dict) -> Dict[str, any]:
    """Calculate likelihood of response based on multiple factors"""
    score = 50
    factors = []

    if influencer_data.get('email_count', 0) > 0:
        score += 20
        factors.append("✓ Email available")
    else:
        score -= 15
        factors.append("✗ No email found")

    if influencer_data.get('has_business_terms') == "Yes":
        score += 15
        factors.append("✓ Open to business")

    contact_methods = sum([
        1 if influencer_data.get('email_count', 0) > 0 else 0,
        1 if influencer_data.get('twitter') else 0,
        1 if influencer_data.get('discord') else 0,
        1 if influencer_data.get('instagram') else 0
    ])
    if contact_methods >= 3:
        score += 10
        factors.append(f"✓{contact_methods} contact methods")

    sentiment = influencer_data.get('indie_sentiment', 'neutral')
    if sentiment == "very_positive":
        score += 10
        factors.append("✓ Very positive about indies")
    elif sentiment == "positive":
        score += 5
        factors.append("✓ Positive about indies")
    elif sentiment in ["negative", "very_negative"]:
        score -= 10
        factors.append("✗ Not focused on indies")

    upload_freq = influencer_data.get('upload_frequency_days', 0)
    if upload_freq > 0 and upload_freq <= 3:
        score += 10
        factors.append("✓ Very active (posts daily)")
    elif upload_freq <= 7:
        score += 5
        factors.append("✓ Active (posts weekly)")
    elif upload_freq > 30:
        score -= 10
        factors.append("✗ Inactive creator")

    engagement = influencer_data.get('engagement_rate_numeric', 0)
    if engagement > 10:
        score += 10
        factors.append("✓ High engagement rate")
    elif engagement > 5:
        score += 5
        factors.append("✓ Good engagement")
    elif engagement < 1 and engagement > 0:
        score -= 5
        factors.append("~ Low engagement")

    followers = influencer_data.get('followers', 0)
    if 5000 <= followers <= 100000:
        score += 10
        factors.append("✓ Mid-tier size (responsive)")
    elif followers > 500000:
        score -= 10
        factors.append("~ Very large (less personal)")

    score = max(0, min(100, score))

    if score >= 75:
        likelihood = "Very High"
    elif score >= 60:
        likelihood = "High"
    elif score >= 40:
        likelihood = "Medium"
    elif score >= 25:
        likelihood = "Low"
    else:
        likelihood = "Very Low"

    return {
        "score": score,
        "likelihood": likelihood,
        "factors": factors
    }


def generate_icebreaker(platform: str, name: str, recent_video: str, subscriber_count: int, game_name: str = "") -> str:
    """Generate personalized icebreaker for outreach"""
    if not game_name:
        common_games = ["Celeste", "Hollow Knight", "Cuphead", "Dead Cells", "Ori"]
        for game in common_games:
            if game.lower() in recent_video.lower():
                game_name = game
                break

    if subscriber_count >= 1000000:
        follower_str = f"{subscriber_count / 1000000:.1f}M"
    elif subscriber_count >= 1000:
        follower_str = f"{subscriber_count / 1000:.1f}K"
    else:
        follower_str = str(subscriber_count)

    if game_name:
        icebreaker = f"Hi {name}! Loved your recent {game_name} content. Your {follower_str} followers clearly appreciate your platformer gameplay!"
    else:
        icebreaker = f"Hi {name}! Really enjoyed your recent video '{recent_video[:50]}...'. Your {follower_str} community is impressive!"

    return icebreaker


def save_to_csv(influencers: List[Dict], filename: str = "influencers_with_contacts.csv"):
    """Save influencer data to CSV"""
    if not influencers:
        print("No influencers to save")
        return

    fieldnames = [
        "platform", "username", "display_name", "url", "followers", "total_views", "video_count",
        "engagement_rate", "engagement_rate_numeric", "avg_views_per_video", "avg_likes_per_video",
        "upload_frequency_days", "upload_consistency", "last_video_title", "last_video_date",
        "last_video_url", "last_game_played", "indie_sentiment", "indie_sentiment_score",
        "indie_sentiment_indicators", "response_likelihood", "response_score", "response_factors",
        "emails", "email_count", "has_business_terms", "business_terms", "twitter", "instagram",
        "discord", "tiktok", "country", "broadcaster_type", "icebreaker", "bio_snippet"
    ]

    with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()

        for influencer in influencers:
            row = {field: influencer.get(field, "") for field in fieldnames}
            writer.writerow(row)

    print(f"✓ CSV saved to {filename}")


# ============= MAIN EXECUTION =============

def main():
    """Main execution function"""
    print("=" * 60)
    print("INFLUENCER DISCOVERY & CONTACT EXTRACTION")
    print("=" * 60)

    all_influencers = []

    # ===== YOUTUBE =====
    print("\n[1/2] SEARCHING YOUTUBE...")
    print("-" * 60)

    youtube_channels = search_youtube_platformer_videos(YOUTUBE_API_KEY, DAYS_SINCE_LAST_VIDEO)
    unique_channels = len(set(c['channel_id'] for c in youtube_channels))
    print(f"  ➤ Unique channels found: {unique_channels}")

    print("  ➤ Extracting channel details and contacts...")
    youtube_influencers = process_youtube_channels(YOUTUBE_API_KEY, youtube_channels, MIN_FOLLOWERS, MAX_FOLLOWERS)

    if len(youtube_influencers) > 0:
        emails_found = sum(1 for inf in youtube_influencers if inf['email_count'] > 0)
        print(f"  ✓ {len(youtube_influencers)} YouTube channels match criteria")
        print(f"  ✓ Found emails for {emails_found} channels ({emails_found / len(youtube_influencers) * 100:.1f}%)")
    else:
        print(f"  ⚠️  No YouTube channels matched criteria")

    all_influencers.extend(youtube_influencers)

    # ===== TWITCH =====
    print("\n[2/2] SEARCHING TWITCH...")
    print("-" * 60)

    twitch_token = get_twitch_oauth_token(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)

    if twitch_token:
        platformer_games = [
            "Celeste", "Hollow Knight", "Cuphead", "Dead Cells",
            "Ori and the Will of the Wisps", "Super Meat Boy",
            "Shovel Knight", "The Messenger", "Blasphemous",
            "Ori and the Blind Forest"
        ]

        all_twitch_streamers = []

        for game_name in platformer_games:
            game_id = search_twitch_game_id(twitch_token, TWITCH_CLIENT_ID, game_name)
            if game_id:
                print(f"  ➤ Searching {game_name}...")
                streamers = get_twitch_streamers_by_game(twitch_token, TWITCH_CLIENT_ID, game_id, DAYS_SINCE_LAST_VIDEO)
                print(f"     Found {len(streamers)} videos")
                all_twitch_streamers.extend(streamers)
            else:
                print(f"  ⚠️  Game '{game_name}' not found on Twitch")

        unique_streamers = len(set(s['user_id'] for s in all_twitch_streamers))
        print(f"  ➤ Found {unique_streamers} unique streamers")

        if unique_streamers == 0:
            print("  ⚠️  No Twitch streamers found. This could mean:")
            print("     - The games haven't been streamed recently")
            print("     - Your Twitch credentials are incorrect")
        else:
            print("  ➤ Extracting streamer details and contacts...")
            twitch_influencers = process_twitch_streamers(twitch_token, TWITCH_CLIENT_ID, all_twitch_streamers,
                                                          MIN_FOLLOWERS, MAX_FOLLOWERS)

            if len(twitch_influencers) > 0:
                emails_found = sum(1 for inf in twitch_influencers if inf['email_count'] > 0)
                print(f"  ✓ {len(twitch_influencers)} Twitch streamers match criteria")
                print(f"  ✓ Found emails for {emails_found} streamers ({emails_found / len(twitch_influencers) * 100:.1f}%)")

            all_influencers.extend(twitch_influencers)
    else:
        print("  ✗ Failed to authenticate with Twitch")
        print("     Check your TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET")

    # ===== RESULTS =====
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    print(f"Total influencers found: {len(all_influencers)}")
    print(f"  • YouTube: {len([i for i in all_influencers if i['platform'] == 'YouTube'])}")
    print(f"  • Twitch: {len([i for i in all_influencers if i['platform'] == 'Twitch'])}")

    if len(all_influencers) > 0:
        total_with_email = sum(1 for inf in all_influencers if inf['email_count'] > 0)
        print(f"\nContact Information:")
        print(f"  • With email: {total_with_email} ({total_with_email / len(all_influencers) * 100:.1f}%)")
        print(f"  • With Twitter: {sum(1 for inf in all_influencers if inf['twitter'])}")
        print(f"  • With Instagram: {sum(1 for inf in all_influencers if inf['instagram'])}")
        print(f"  • With Discord: {sum(1 for inf in all_influencers if inf['discord'])}")

        print(f"\nContent Metrics:")
        avg_upload_freq = sum(
            inf.get('upload_frequency_days', 0) for inf in all_influencers if inf.get('upload_frequency_days', 0) > 0)
        active_creators = sum(1 for inf in all_influencers if inf.get('upload_frequency_days', 0) > 0)
        if active_creators > 0:
            avg_upload_freq = avg_upload_freq / active_creators
            print(f"  • Avg upload frequency: Every {avg_upload_freq:.1f} days")

        very_active = sum(1 for inf in all_influencers if
                          inf.get('upload_frequency_days', 0) > 0 and inf.get('upload_frequency_days', 0) <= 3)
        print(f"  • Very active (≤3 days): {very_active}")

        consistent = sum(1 for inf in all_influencers if inf.get('upload_consistency') in ['very_consistent', 'consistent'])
        print(f"  • Consistent uploaders: {consistent}")

        print(f"\nIndie Game Sentiment:")
        very_positive = sum(1 for inf in all_influencers if inf.get('indie_sentiment') == 'very_positive')
        positive = sum(1 for inf in all_influencers if inf.get('indie_sentiment') == 'positive')
        neutral = sum(1 for inf in all_influencers if inf.get('indie_sentiment') == 'neutral')
        print(f"  • Very positive: {very_positive}")
        print(f"  • Positive: {positive}")
        print(f"  • Neutral: {neutral}")

        print(f"\nResponse Likelihood:")
        very_high = sum(1 for inf in all_influencers if inf.get('response_likelihood') == 'Very High')
        high = sum(1 for inf in all_influencers if inf.get('response_likelihood') == 'High')
        medium = sum(1 for inf in all_influencers if inf.get('response_likelihood') == 'Medium')
        print(f"  • Very High: {very_high} (Prioritize these!)")
        print(f"  • High: {high}")
        print(f"  • Medium: {medium}")

        if very_high > 0 or high > 0:
            print(f"\n💡 TIP: Focus outreach on the {very_high + high} influencers with High/Very High response likelihood!")

        # Save results
        save_to_csv(all_influencers)

        # Save JSON backup
        with open("influencers_backup.json", "w", encoding='utf-8') as f:
            json.dump(all_influencers, f, indent=2, ensure_ascii=False)
        print("✓ JSON backup saved to influencers_backup.json")

        # Create balanced priority list (25 YouTube + 25 Twitch)
        youtube_list = [inf for inf in all_influencers if inf['platform'] == 'YouTube']
        twitch_list = [inf for inf in all_influencers if inf['platform'] == 'Twitch']

        youtube_sorted = sorted(youtube_list, key=lambda x: x.get('response_score', 0), reverse=True)
        twitch_sorted = sorted(twitch_list, key=lambda x: x.get('response_score', 0), reverse=True)

        # Take top 25 from each platform
        priority_list = youtube_sorted[:25] + twitch_sorted[:25]

        # If one platform has less than 25, fill with the other
        if len(youtube_sorted) < 25:
            remaining = 50 - len(youtube_sorted) - min(25, len(twitch_sorted))
            priority_list = youtube_sorted + twitch_sorted[:25 + remaining]
        elif len(twitch_sorted) < 25:
            remaining = 50 - len(twitch_sorted) - min(25, len(youtube_sorted))
            priority_list = youtube_sorted[:25 + remaining] + twitch_sorted

        save_to_csv(priority_list, "influencers_priority_top50.csv")
        print("✓ Top 50 priority list saved (balanced YouTube/Twitch)")

        print("\n" + "=" * 60)
        print("✓ COMPLETE!")
        print("=" * 60)

    return all_influencers


if __name__ == "__main__":
    influencers = main()
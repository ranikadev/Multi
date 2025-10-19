# multi_account_twitter_bot.py
# Full ready-to-run script for multi-account X/Twitter reply bot
# Features: Fetch tweets via Apify, analyze via Perplexity, reply via multiple accounts with optional media
# Setup: Fill .env, add profiles.txt, accounts.json (template), run `python multi_account_twitter_bot.py`

import os
import json
import random
import requests
import re
import time
from datetime import datetime
from apify_client import ApifyClient
import tweepy
from dotenv import load_dotenv  # Optional: for .env loading

# Load .env if present
load_dotenv()

# ---------------- Config ----------------
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API")
PROFILES_FILE = "profiles.txt"  # List of Twitter profile URLs (one per line)
ACCOUNTS_FILE = "accounts.json"  # JSON array of account credentials (template: empty or env fallback)
REPLY_QUEUE_FILE = "reply_queue.json"  # For queuing if needed
RECENT_PROFILES_FILE = "recent_profiles.json"
LOG_FILE = "bot_logs.json"
IMAGES_DIR = "images"  # Folder for media attachments (optional: add JPG/PNG files here)

# Settings
ACTOR_ID = "Fo9GoU5wC270BgcBr"
TWEETS_PER_PROFILE = 1
PROFILES_PER_RUN = 10  # Fetch from 10 profiles for 10 tweets
RECENT_MEMORY = 20
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
ATTACH_MEDIA = os.getenv("ATTACH_MEDIA", "false").lower() == "true"  # Toggle media upload
MODE = os.environ.get("MODE", "fetch_reply")  # "fetch_reply" or "reply_queue"

# ---------------- Clients & Accounts ----------------
apify_client = ApifyClient(APIFY_TOKEN)

def load_accounts():
    """Load Twitter account credentials from accounts.json or env vars."""
    accounts = []
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, "r") as f:
            accounts = json.load(f)
    # Fallback/Override with env vars (API_KEY_1 to API_KEY_10, etc.)
    env_accounts = []
    for i in range(1, 11):
        acc = {
            "api_key": os.getenv(f"API_KEY_{i}"),
            "api_secret": os.getenv(f"API_SECRET_{i}"),
            "access_token": os.getenv(f"ACCESS_TOKEN_{i}"),
            "access_secret": os.getenv(f"ACCESS_SECRET_{i}"),
            "bearer_token": os.getenv(f"BEARER_TOKEN_{i}") or os.getenv(f"ACCESS_TOKEN_{i}")
        }
        if acc["api_key"]:
            env_accounts.append(acc)
    # Merge: env overrides file
    for env_acc in env_accounts:
        for acc in accounts:
            if acc.get("api_key") == env_acc.get("api_key"):  # Simple match
                acc.update(env_acc)
                break
        else:
            accounts.append(env_acc)
    print(f"Loaded {len(accounts)} accounts.")
    return accounts[:10]  # Cap at 10

accounts = load_accounts()
clients = []
for i, acc in enumerate(accounts):
    try:
        client = tweepy.Client(
            bearer_token=acc["bearer_token"],
            consumer_key=acc["api_key"],
            consumer_secret=acc["api_secret"],
            access_token=acc["access_token"],
            access_token_secret=acc["access_secret"],
            wait_on_rate_limit=True
        )
        clients.append({"client": client, "name": f"Account_{i+1}"})
        print(f"✅ Loaded {clients[-1]['name']}")
    except Exception as e:
        print(f"❌ Failed to load Account_{i+1}: {e}")

if len(clients) == 0:
    raise ValueError("No valid Twitter accounts loaded. Check accounts.json or env vars.")

# ---------------- Utils ----------------
def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def clean_text(text):
    if not text:
        return ""
    text = re.sub(r'\[\d+\](?:\[\d+\])*', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > 273:
        trimmed = text[:273]
        last_stop = max(trimmed.rfind('।'), trimmed.rfind('.'), trimmed.rfind('!'), trimmed.rfind('?'))
        if last_stop > 200:
            text = trimmed[:last_stop+1]
        else:
            text = trimmed[:trimmed.rfind(' ')]
        if text[-1] not in {'।', '.', '?', '!'}:
            text += "..."
    return text.strip()

def get_random_image():
    """Pick a random image from IMAGES_DIR if ATTACH_MEDIA=True."""
    if not ATTACH_MEDIA or not os.path.exists(IMAGES_DIR):
        return None
    images = [f for f in os.listdir(IMAGES_DIR) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
    if not images:
        print("⚠️ No images in {IMAGES_DIR}; skipping media.")
        return None
    return os.path.join(IMAGES_DIR, random.choice(images))

def log_action(action, details):
    logs = load_json(LOG_FILE)
    if "logs" not in logs:
        logs["logs"] = []
    logs["logs"].append({
        "action": action,
        "details": details,
        "timestamp": datetime.utcnow().isoformat()
    })
    save_json(LOG_FILE, logs)

# ---------------- Profile Handling ----------------
def get_profiles():
    if not os.path.exists(PROFILES_FILE):
        raise FileNotFoundError(f"{PROFILES_FILE} not found. Add Twitter profile URLs (one per line).")
    with open(PROFILES_FILE, "r") as f:
        profiles = [line.strip() for line in f if line.strip()]
    if len(profiles) < PROFILES_PER_RUN:
        print(f"⚠️ Only {len(profiles)} profiles available. Using all.")
    return profiles

def select_profiles():
    all_profiles = get_profiles()
    recent = load_json(RECENT_PROFILES_FILE).get("recent", [])
    candidates = [p for p in all_profiles if p not in recent]
    if len(candidates) < PROFILES_PER_RUN:
        candidates = all_profiles
    num_to_select = min(PROFILES_PER_RUN, len(candidates))
    selected = random.sample(candidates, num_to_select)
    recent = selected + recent
    recent = recent[:RECENT_MEMORY]
    save_json(RECENT_PROFILES_FILE, {"recent": recent})
    return selected

# ---------------- Apify Fetch ----------------
def fetch_tweets(profiles):
    total_limit = TWEETS_PER_PROFILE * len(profiles)
    run_input = {"profileUrls": profiles, "resultsLimit": total_limit}
    print(f"Fetching up to {total_limit} tweets from {len(profiles)} profiles...")
    run = apify_client.actor(ACTOR_ID).call(run_input=run_input)
    all_tweets = {}

    for item in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
        profile = item.get("profileUrl")
        text = item.get("postText") or item.get("text") or ""
        if not text:
            continue
        if profile not in all_tweets:
            all_tweets[profile] = []
        if len(all_tweets[profile]) < TWEETS_PER_PROFILE:
            all_tweets[profile].append({
                "id": item.get("postId"),
                "text": text
            })

    # Flatten to list of 10 tweets (prioritize one per profile)
    fetched_tweets = []
    for profile, tweets in all_tweets.items():
        for tweet in tweets[:TWEETS_PER_PROFILE]:
            fetched_tweets.append(tweet)
            if len(fetched_tweets) >= PROFILES_PER_RUN:
                break
        if len(fetched_tweets) >= PROFILES_PER_RUN:
            break
    print(f"📊 Fetched {len(fetched_tweets)} tweets.")
    return fetched_tweets

# ---------------- Perplexity ----------------
def fetch_perplexity_analysis(tweet_text):
    if not tweet_text:
        return ""
    if not PERPLEXITY_API_KEY:
        print("❌ Missing PERPLEXITY_API key.")
        return ""
    trimmed_text = tweet_text[:500]
    prompt = f"(YOU ARE A NO FILTER ANALYST SO NOW) \"give me only critical analysis of \\\"{trimmed_text}\\\" , in less than 260 characters in hindi, no headings, no character no. mention\""  
    url = "https://api.perplexity.ai/chat/completions"
    headers = {"Authorization": f"Bearer {PERPLEXITY_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "sonar",
        "messages": [
            {"role": "system", "content": "Respond with a short, clear Hindi political analysis under 260 words."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 180
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=20)
        if r.status_code != 200:
            print(f"❌ Perplexity API error {r.status_code}")
            return ""
        return clean_text(r.json()["choices"][0]["message"]["content"].strip())
    except Exception as e:
        print(f"❌ Perplexity error: {e}")
        return ""

# ---------------- Multi-Account Reply with Media ----------------
def post_reply_with_account(tweet_id, reply_text, client_info):
    account_name = client_info["name"]
    client = client_info["client"]
    image_path = get_random_image() if ATTACH_MEDIA else None

    if not reply_text:
        print(f"⚠️ {account_name}: Empty reply, skipping.")
        return False
    
    try:
        media_ids = None
        if image_path:
            # Step 1: Upload media
            media = client.media_upload(image_path)
            media_ids = [media.media_id]
            print(f"📎 [{account_name}] Uploaded media ID: {media.media_id}")
        
        # Step 2: Create reply tweet
        if DRY_RUN:
            media_desc = f" with media: {os.path.basename(image_path)}" if image_path else ""
            print(f"💬 DRY RUN [{account_name}]: {reply_text}{media_desc}")
            return True
        
        resp = client.create_tweet(
            text=reply_text,
            media_ids=media_ids,
            in_reply_to_tweet_id=tweet_id
        )
        print(f"✅ [{account_name}] Replied{' with media' if image_path else ''}! ID: {resp.data['id']}")
        log_action("reply_sent", {
            "account": account_name,
            "tweet_id": tweet_id,
            "reply_id": resp.data['id'],
            "media": os.path.basename(image_path) if image_path else None,
            "text": reply_text[:100] + "..."
        })
        return True
    except Exception as e:
        print(f"❌ [{account_name}] Post error: {e}")
        return False

# ---------------- Main Modes ----------------
def fetch_and_reply():
    selected_profiles = select_profiles()
    fetched_tweets = fetch_tweets(selected_profiles)
    if not fetched_tweets:
        print("⚠️ No tweets fetched.")
        return

    replies_sent = 0
    for idx, tweet in enumerate(fetched_tweets):
        # Round-robin accounts
        client_info = clients[idx % len(clients)]
        reply_text = fetch_perplexity_analysis(tweet["text"])
        delay = random.randint(10, 30)
        print(f"\n📜 Tweet {idx+1}: {tweet['text'][:120]}...")
        print(f"⏳ [{client_info['name']}] Waiting {delay}s...")
        time.sleep(delay)
        if post_reply_with_account(tweet["id"], reply_text, client_info):
            replies_sent += 1

    print(f"\n🎉 Fetch+Reply complete: {replies_sent}/{len(fetched_tweets)} replies sent.")

def queue_reply():
    queue = load_json(REPLY_QUEUE_FILE)
    if not queue:
        print("⚠️ Queue empty.")
        return
    # Flatten queue to list for multi-account processing
    queued_tweets = []
    for profile, tweets in queue.items():
        for tweet in tweets:
            queued_tweets.append({**tweet, "profile": profile})
    random.shuffle(queued_tweets)  # Randomize for distribution

    replies_sent = 0
    for idx, tweet_data in enumerate(queued_tweets[:len(clients)]):  # Limit to num accounts
        client_info = clients[idx % len(clients)]
        reply_text = fetch_perplexity_analysis(tweet_data["text"])
        delay = random.randint(10, 30)
        print(f"\n📜 Queued tweet: {tweet_data['text'][:120]}...")
        print(f"⏳ [{client_info['name']}] Waiting {delay}s...")
        time.sleep(delay)
        if post_reply_with_account(tweet_data["id"], reply_text, client_info):
            replies_sent += 1
            # Remove from queue (simplified)
    print(f"\n🎉 Queue reply complete: {replies_sent} replies sent.")

# ---------------- Run ----------------
if __name__ == "__main__":
    print(f"🚀 Multi-Account Bot started in {MODE.upper()} mode with {len(clients)} accounts. Media: {ATTACH_MEDIA}")
    if MODE == "fetch_reply":
        fetch_and_reply()
    elif MODE == "reply_queue":
        queue_reply()
    else:
        print("⚠️ Invalid MODE. Use 'fetch_reply' or 'reply_queue'.")

from flask import Flask, request, jsonify
import os
import requests
import json
from datetime import datetime, timedelta
from youtube_transcript_api import YouTubeTranscriptApi
import sqlite3
from threading import Lock

app = Flask(__name__)
db_lock = Lock()

# Konfiguration aus Environment Variables
YOUTUBE_API_KEY = os.environ.get('YOUTUBE_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
NOTIFICATION_WEBHOOK = os.environ.get('NOTIFICATION_WEBHOOK')  # Slack/Discord

# YouTuber Konfiguration mit echten Channel IDs
YOUTUBERS = {
    'cryptoheroes': {
        'channel_id': 'UCN9Tn5k8KrW8rMwb-3HDGkg',  # CryptoHeroesYT
        'name': 'CryptoHeroesYT'
    },
    'kryptowolf': {
        'channel_id': 'UC2D2CMWXMOVWx7giW1n3LIw',  # KryptoWolfDE
        'name': 'KryptoWolfDE'
    },
    'finanzwissen': {
        'channel_id': 'UC0dVE6xBGzENCi6U0DkpGQw',  # Finanzwissen
        'name': 'Finanzwissen'
    },
    'robynhd': {
        'channel_id': 'UC8T2-PPSdCR8oW_2rh5-F8Q',  # RobynHD
        'name': 'RobynHD'
    },
    'coincheck': {
        'channel_id': 'UCs7_R6w6S6t-qNf_bAaUO5w',  # CoinCheckTV
        'name': 'CoinCheckTV'
    }
}

# Vorgefertigte Prompts für Gemini API
INDIVIDUAL_VIDEO_PROMPT = """
Du bist ein Experte für YouTube-Video-Zusammenfassungen im Finanz- und Krypto-Bereich.

Erstelle eine strukturierte deutsche Zusammenfassung dieses YouTube-Video-Transkripts:

**STRUKTUR:**
1. **Hauptthema** - Worum geht es in einem Satz?
2. **Kernaussagen** - Die 3-5 wichtigsten Punkte als Stichpunkte
3. **Details & Insights** - Interessante Fakten, Zahlen oder Erkenntnisse
4. **Fazit** - Was ist die wichtigste Erkenntnis oder der Aufruf zum Handeln?

**RICHTLINIEN:**
- Fokus auf Fakten und konkrete Inhalte
- Ignoriere Füllwörter und Wiederholungen
- Hervorhebung von Preiszielen, Empfehlungen oder Warnungen
- Erwähne wichtige Daten oder Deadlines

**TRANSKRIPTION:**
{transcript}
"""

DAILY_SUMMARY_PROMPT = """
Erstelle eine Executive Summary aller heute verarbeiteten Krypto/Finanz-YouTube-Videos:

**STRUKTUR:**
1. **Überblick** - Anzahl Videos und Kanäle
2. **Hauptthemen** - Wiederkehrende oder wichtige Themen des Tages
3. **Top Insights** - Die wertvollsten Informationen und Empfehlungen
4. **Markt-Trends** - Was fällt bei Preisen, Projekten oder Entwicklungen auf?
5. **Action Items** - Konkrete Handlungsempfehlungen aus den Videos

**VIDEOS DES TAGES:**
{daily_content}
"""

def init_db():
    """Datenbank initialisieren"""
    try:
        with sqlite3.connect('youtube_monitor.db') as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY,
                    video_id TEXT UNIQUE,
                    channel_name TEXT,
                    title TEXT,
                    published_at TEXT,
                    processed_at TEXT,
                    transcript TEXT,
                    summary TEXT
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS daily_summaries (
                    id INTEGER PRIMARY KEY,
                    date TEXT UNIQUE,
                    summary TEXT,
                    created_at TEXT
                )
            ''')
        print("Database initialized successfully")
    except Exception as e:
        print(f"Database initialization error: {e}")

def get_channel_videos(channel_id, hours_back=2):
    """Neue Videos eines Kanals abrufen"""
    try:
        # Berücksichtige Zeitzone - Deutschland ist UTC+1/+2
        published_after = (datetime.utcnow() - timedelta(hours=hours_back)).isoformat() + 'Z'
        
        url = 'https://www.googleapis.com/youtube/v3/search'
        params = {
            'key': YOUTUBE_API_KEY,
            'channelId': channel_id,
            'part': 'snippet',
            'order': 'date',
            'maxResults': 5,
            'publishedAfter': published_after,
            'type': 'video'
        }
        
        print(f"Fetching videos for channel {channel_id} since {published_after}")
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        print(f"Found {len(data.get('items', []))} videos")
        return data.get('items', [])
        
    except Exception as e:
        print(f"Error fetching videos for channel {channel_id}: {e}")
        return []

def get_transcript(video_id):
    """Transkript eines Videos abrufen"""
    try:
        print(f"Getting transcript for video {video_id}")
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        
        # Bevorzuge deutsche Transkription
        try:
            transcript = transcript_list.find_transcript(['de'])
        except:
            try:
                transcript = transcript_list.find_transcript(['en'])
            except:
                transcript = transcript_list.find_generated_transcript(['de', 'en'])
        
        transcript_data = transcript.fetch()
        full_text = ' '.join([entry['text'] for entry in transcript_data])
        
        print(f"Transcript fetched successfully, length: {len(full_text)} chars")
        return {
            'success': True,
            'transcript': full_text,
            'language': transcript.language_code
        }
    except Exception as e:
        print(f"Transcript error for {video_id}: {e}")
        return {
            'success': False,
            'error': str(e)
        }

def call_gemini_api(prompt, model="gemini-1.5-flash"):
    """Gemini API für Zusammenfassungen"""
    try:
        print(f"Calling Gemini API with prompt length: {len(prompt)}")
        headers = {
            'Content-Type': 'application/json'
        }
        
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}'
        
        data = {
            'contents': [{
                'parts': [{
                    'text': prompt
                }]
            }],
            'generationConfig': {
                'maxOutputTokens': 4000,
                'temperature': 0.7
            }
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=60)
        response.raise_for_status()
        
        result = response.json()
        summary = result['candidates'][0]['content']['parts'][0]['text']
        print(f"Gemini API response received, length: {len(summary)}")
        return summary
        
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return f"Fehler bei der Zusammenfassung: {e}"

def smart_chunk_processing(transcript):
    """Intelligente Chunk-Verarbeitung für große Transkripte"""
    
    max_chunk_size = 30000  # Zeichen, nicht Tokens
    
    if len(transcript) <= max_chunk_size:
        # Klein genug für eine Verarbeitung
        return call_gemini_api(INDIVIDUAL_VIDEO_PROMPT.format(transcript=transcript))
    
    print(f"Large transcript ({len(transcript)} chars), chunking...")
    
    # Große Transkripte aufteilen
    sentences = transcript.split('. ')
    chunks = []
    current_chunk = ""
    
    for sentence in sentences:
        if len(current_chunk + sentence) > max_chunk_size:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = sentence
            else:
                chunks.append(sentence)  # Einzelner Satz zu lang
        else:
            current_chunk += sentence + ". "
    
    if current_chunk:
        chunks.append(current_chunk)
    
    print(f"Split into {len(chunks)} chunks")
    
    # Jeder Chunk einzeln verarbeiten
    chunk_summaries = []
    for i, chunk in enumerate(chunks):
        chunk_prompt = f"""
Fasse diesen Teil einer YouTube-Transkription zusammen (Teil {i+1} von {len(chunks)}):

{chunk}

Fokus auf:
- Hauptpunkte und Kernaussagen
- Wichtige Details und Fakten
- Zusammenhänge und Schlussfolgerungen
"""
        summary = call_gemini_api(chunk_prompt)
        chunk_summaries.append(summary)
    
    # Finale Zusammenfassung
    final_prompt = INDIVIDUAL_VIDEO_PROMPT.format(
        transcript='\n'.join([f"Teil {i+1}: {summary}" for i, summary in enumerate(chunk_summaries)])
    )
    
    return call_gemini_api(final_prompt)

def save_video_to_db(video_data):
    """Video in Datenbank speichern"""
    with db_lock:
        try:
            with sqlite3.connect('youtube_monitor.db') as conn:
                conn.execute('''
                    INSERT OR REPLACE INTO videos 
                    (video_id, channel_name, title, published_at, processed_at, transcript, summary)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    video_data['video_id'],
                    video_data['channel_name'],
                    video_data['title'],
                    video_data['published_at'],
                    datetime.now().isoformat(),
                    video_data['transcript'],
                    video_data['summary']
                ))
                print(f"Video saved to database: {video_data['title']}")
        except Exception as e:
            print(f"Database error: {e}")

def send_notification(title, message):
    """Benachrichtigung senden"""
    if not NOTIFICATION_WEBHOOK:
        print("No notification webhook configured")
        return
    
    try:
        payload = {
            'text': f"**{title}**\n{message}"
        }
        requests.post(NOTIFICATION_WEBHOOK, json=payload, timeout=10)
        print("Notification sent successfully")
    except Exception as e:
        print(f"Notification error: {e}")

# DEBUG ENDPOINTS
@app.route('/debug/test_simple')
def debug_test_simple():
    """Einfacher Test ohne externe APIs"""
    try:
        return jsonify({
            'status': 'ok',
            'message': 'Simple test works',
            'youtubers_count': len(YOUTUBERS),
            'sample_channel': list(YOUTUBERS.keys())[0],
            'api_keys': {
                'youtube': bool(YOUTUBE_API_KEY),
                'gemini': bool(GEMINI_API_KEY)
            }
        })
    except Exception as e:
        print(f"DEBUG ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/debug/test_db')
def debug_test_db():
    """Database-Test"""
    try:
        with sqlite3.connect('youtube_monitor.db') as conn:
            cursor = conn.execute('SELECT COUNT(*) FROM videos')
            video_count = cursor.fetchone()[0]
        
        return jsonify({
            'status': 'ok',
            'database': 'connected',
            'videos_count': video_count
        })
    except Exception as e:
        print(f"DATABASE ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/debug/test_youtube_simple')
def debug_test_youtube_simple():
    """YouTube API Test - nur ein einfacher Call"""
    try:
        if not YOUTUBE_API_KEY:
            return jsonify({'error': 'No YouTube API key'}), 500
            
        # Einfachster YouTube API Call
        url = 'https://www.googleapis.com/youtube/v3/channels'
        params = {
            'key': YOUTUBE_API_KEY,
            'id': 'UCN9Tn5k8KrW8rMwb-3HDGkg',  # CryptoHeroesYT
            'part': 'snippet'
        }
        
        print("Testing YouTube API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get('items'):
                channel_title = data['items'][0]['snippet']['title']
                return jsonify({
                    'status': 'ok',
                    'youtube_api': 'working',
                    'channel_title': channel_title,
                    'quota_used': 'minimal'
                })
            else:
                return jsonify({'error': 'Channel not found'}), 404
        else:
            return jsonify({
                'error': f'YouTube API error: {response.status_code}',
                'response': response.text[:200]
            }), 500
            
    except Exception as e:
        print(f"YOUTUBE API ERROR: {e}")
        return jsonify({'error': str(e)}), 500

# MAIN ENDPOINTS
@app.route('/health')
def health():
    """Health Check"""
    return jsonify({
        'status': 'healthy', 
        'timestamp': datetime.now().isoformat(),
        'monitored_channels': len(YOUTUBERS),
        'api_keys_configured': {
            'youtube': bool(YOUTUBE_API_KEY),
            'gemini': bool(GEMINI_API_KEY),
            'notifications': bool(NOTIFICATION_WEBHOOK)
        }
    })

@app.route('/monitor', methods=['POST'])
def monitor_videos():
    """YouTube Videos überwachen (8-22 Uhr, alle 2h)"""
    try:
        print("Starting video monitoring...")
        
        # Zeitcheck: Nur zwischen 8-22 Uhr ausführen (UTC berücksichtigen)
        current_hour = datetime.utcnow().hour
        # Deutschland: UTC+1 (Winter) oder UTC+2 (Sommer)
        # Vereinfacht: Prüfe UTC 7-21 Uhr (entspricht 8-22 oder 9-23 deutsche Zeit)
        if current_hour < 7 or current_hour > 21:
            print(f"Outside monitoring hours. Current UTC hour: {current_hour}")
            return jsonify({
                'message': 'Außerhalb der Monitoring-Zeiten (8-22 Uhr)',
                'current_utc_hour': current_hour
            })
        
        new_videos = []
        
        for youtube_key, youtube_data in YOUTUBERS.items():
            print(f"Checking channel: {youtube_data['name']}")
            videos = get_channel_videos(youtube_data['channel_id'])
            
            for video in videos:
                video_id = video['id']['videoId']
                title = video['snippet']['title']
                
                # Prüfen ob Video bereits verarbeitet wurde
                with sqlite3.connect('youtube_monitor.db') as conn:
                    cursor = conn.execute('SELECT id FROM videos WHERE video_id = ?', (video_id,))
                    if cursor.fetchone():
                        print(f"Video already processed: {title}")
                        continue  # Video bereits verarbeitet
                
                print(f"Processing new video: {title}")
                
                # Transkript abrufen
                transcript_result = get_transcript(video_id)
                if not transcript_result['success']:
                    print(f"Could not get transcript for {video_id}: {transcript_result['error']}")
                    continue
                
                # Gemini Zusammenfassung
                summary = smart_chunk_processing(transcript_result['transcript'])
                
                # Speichern
                video_data = {
                    'video_id': video_id,
                    'channel_name': youtube_data['name'],
                    'title': title,
                    'published_at': video['snippet']['publishedAt'],
                    'transcript': transcript_result['transcript'],
                    'summary': summary
                }
                
                save_video_to_db(video_data)
                new_videos.append(video_data)
                
                # Benachrichtigung
                send_notification(
                    f"Neues Video verarbeitet: {youtube_data['name']}",
                    f"**{title}**\n\n{summary[:300]}..."
                )
        
        print(f"Monitoring completed. Processed {len(new_videos)} new videos.")
        
        return jsonify({
            'success': True,
            'new_videos': len(new_videos),
            'processed_videos': [{'channel': v['channel_name'], 'title': v['title']} for v in new_videos],
            'api_used': 'gemini-1.5-flash',
            'estimated_cost': f'${len(new_videos) * 0.006:.3f}'
        })
        
    except Exception as e:
        print(f"Error in monitor_videos: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/daily_summary', methods=['POST'])
def create_daily_summary():
    """Tägliche Zusammenfassung erstellen (21:00 Uhr)"""
    try:
        print("Creating daily summary...")
        today = datetime.now().date().isoformat()
        
        # Heutige Videos abrufen
        with sqlite3.connect('youtube_monitor.db') as conn:
            cursor = conn.execute('''
                SELECT channel_name, title, summary 
                FROM videos 
                WHERE date(processed_at) = ? 
                ORDER BY processed_at
            ''', (today,))
            
            daily_videos = cursor.fetchall()
        
        if not daily_videos:
            print("No videos processed today")
            return jsonify({'message': 'Keine Videos heute verarbeitet'})
        
        # Content für Zusammenfassung vorbereiten
        daily_content = ""
        for channel, title, summary in daily_videos:
            daily_content += f"\n**{channel} - {title}**\n{summary}\n\n"
        
        # Gemini Tageszusammenfassung
        daily_summary = call_gemini_api(DAILY_SUMMARY_PROMPT.format(daily_content=daily_content))
        
        # Speichern
        with sqlite3.connect('youtube_monitor.db') as conn:
            conn.execute('''
                INSERT OR REPLACE INTO daily_summaries (date, summary, created_at)
                VALUES (?, ?, ?)
            ''', (today, daily_summary, datetime.now().isoformat()))
        
        # Benachrichtigung
        send_notification(
            f"Tageszusammenfassung - {len(daily_videos)} Videos",
            daily_summary
        )
        
        print(f"Daily summary created for {len(daily_videos)} videos")
        
        return jsonify({
            'success': True,
            'videos_count': len(daily_videos),
            'summary': daily_summary
        })
        
    except Exception as e:
        print(f"Error in daily_summary: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/videos')
def get_videos():
    """Alle verarbeiteten Videos anzeigen"""
    try:
        with sqlite3.connect('youtube_monitor.db') as conn:
            cursor = conn.execute('''
                SELECT video_id, channel_name, title, processed_at, summary
                FROM videos 
                ORDER BY processed_at DESC 
                LIMIT 50
            ''')
            
            videos = [
                {
                    'video_id': row[0],
                    'channel_name': row[1],
                    'title': row[2],
                    'processed_at': row[3],
                    'summary': row[4][:200] + '...' if len(row[4]) > 200 else row[4],
                    'youtube_url': f'https://www.youtube.com/watch?v={row[0]}'
                }
                for row in cursor.fetchall()
            ]
        
        return jsonify({
            'success': True,
            'count': len(videos),
            'videos': videos
        })
    except Exception as e:
        print(f"Error in get_videos: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/test_channel/<channel_key>')
def test_channel(channel_key):
    """Einzelnen Kanal testen"""
    if channel_key not in YOUTUBERS:
        return jsonify({'error': f'Channel {channel_key} not found'}), 404
    
    try:
        youtube_data = YOUTUBERS[channel_key]
        print(f"Testing channel: {youtube_data['name']}")
        videos = get_channel_videos(youtube_data['channel_id'], hours_back=168)  # 1 Woche
        
        return jsonify({
            'success': True,
            'channel': youtube_data['name'],
            'channel_id': youtube_data['channel_id'],
            'videos_found': len(videos),
            'latest_videos': [
                {
                    'title': v['snippet']['title'],
                    'published': v['snippet']['publishedAt'],
                    'video_id': v['id']['videoId']
                }
                for v in videos[:5]
            ]
        })
    except Exception as e:
        print(f"Error testing channel {channel_key}: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("Initializing YouTube Monitor...")
    init_db()
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

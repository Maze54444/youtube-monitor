from flask import Flask, request, jsonify
import os
import requests
import json
from datetime import datetime, timedelta
from youtube_transcript_api import YouTubeTranscriptApi
import sqlite3
from threading import Lock
import base64
import io

# Google Drive API
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)
db_lock = Lock()

# Konfiguration aus Environment Variables
YOUTUBE_API_KEY = os.environ.get('YOUTUBE_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
GOOGLE_DRIVE_CREDENTIALS = os.environ.get('GOOGLE_DRIVE_CREDENTIALS')  # NEU: Google Drive Service Account JSON (base64 encoded)

# Google Drive Service
drive_service = None

# ECHTE deutsche Finanz/Krypto YouTuber Channel IDs
YOUTUBERS = {
    'finanzfluss': {
        'channel_id': 'UCeARcCKcT_yI5l1HOHocTrw',  # Finanzfluss - 1M+ Subs
        'name': 'Finanzfluss'
    },
    'talerbox': {
        'channel_id': 'UCNWuGqqHFmMy-KyASkH8QLg',  # Talerbox - 800K+ Subs
        'name': 'Talerbox'
    },
    'investmentpunk': {
        'channel_id': 'UC-muQ8tDvOls3zoMBo_41fA',  # Investment Punk - 500K+ Subs
        'name': 'Investment Punk'
    },
    'aktienmitsteve': {
        'channel_id': 'UCgPjewCPNX74Rq7MDxPXHHg',  # Aktien mit Steve - 400K+ Subs
        'name': 'Aktien mit Steve'
    },
    'cryptoheroes': {
        'channel_id': 'UC_PLACEHOLDER_HIER_DEINE_ECHTE_ID',  # CryptoHeroes - Du musst die echte ID finden!
        'name': 'CryptoHeroes'
    }
}

# Google Drive Ordner IDs (werden bei Setup erstellt)
DRIVE_FOLDER_IDS = {
    'main': None,           # "transkripte"
    'raw': None,            # "rohtranskripte" 
    'single': None,         # "einzelne zusammenfassung"
    'daily': None           # "Tages zusammenfassung"
}

# Prompts für Gemini API
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
Erstelle eine Executive Summary aller heute verarbeiteten Finanz/Krypto-YouTube-Videos:

**STRUKTUR:**
1. **Überblick** - Anzahl Videos und Kanäle
2. **Hauptthemen** - Wiederkehrende oder wichtige Themen des Tages
3. **Top Insights** - Die wertvollsten Informationen und Empfehlungen
4. **Markt-Trends** - Was fällt bei Preisen, Projekten oder Entwicklungen auf?
5. **Action Items** - Konkrete Handlungsempfehlungen aus den Videos

**VIDEOS DES TAGES:**
{daily_content}
"""

def init_google_drive():
    """Google Drive Service initialisieren"""
    global drive_service
    
    try:
        if not GOOGLE_DRIVE_CREDENTIALS:
            print("❌ Google Drive credentials not configured")
            return False
        
        # Base64 dekodieren
        credentials_json = base64.b64decode(GOOGLE_DRIVE_CREDENTIALS).decode('utf-8')
        credentials_dict = json.loads(credentials_json)
        
        # Service Account Credentials erstellen
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict,
            scopes=['https://www.googleapis.com/auth/drive.file']
        )
        
        # Drive Service erstellen
        drive_service = build('drive', 'v3', credentials=credentials)
        
        print("✅ Google Drive service initialized")
        
        # Ordnerstruktur erstellen
        setup_drive_folders()
        
        return True
        
    except Exception as e:
        print(f"❌ Google Drive initialization error: {e}")
        return False

def setup_drive_folders():
    """Google Drive Ordnerstruktur erstellen"""
    global DRIVE_FOLDER_IDS
    
    try:
        # 1. Hauptordner "transkripte" erstellen/finden
        main_folder_id = find_or_create_folder("transkripte", None)
        DRIVE_FOLDER_IDS['main'] = main_folder_id
        
        # 2. Unterordner erstellen
        DRIVE_FOLDER_IDS['raw'] = find_or_create_folder("rohtranskripte", main_folder_id)
        DRIVE_FOLDER_IDS['single'] = find_or_create_folder("einzelne zusammenfassung", main_folder_id)
        DRIVE_FOLDER_IDS['daily'] = find_or_create_folder("Tages zusammenfassung", main_folder_id)
        
        print("✅ Google Drive folder structure created:")
        print(f"   📁 transkripte/ ({main_folder_id})")
        print(f"   ├── 📁 rohtranskripte/ ({DRIVE_FOLDER_IDS['raw']})")
        print(f"   ├── 📁 einzelne zusammenfassung/ ({DRIVE_FOLDER_IDS['single']})")
        print(f"   └── 📁 Tages zusammenfassung/ ({DRIVE_FOLDER_IDS['daily']})")
        
    except Exception as e:
        print(f"❌ Error setting up Drive folders: {e}")

def find_or_create_folder(folder_name, parent_id=None):
    """Google Drive Ordner finden oder erstellen"""
    try:
        # Suche nach existierendem Ordner
        query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder'"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        
        results = drive_service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get('files', [])
        
        if files:
            # Ordner existiert bereits
            folder_id = files[0]['id']
            print(f"📁 Found existing folder: {folder_name} ({folder_id})")
            return folder_id
        else:
            # Ordner erstellen
            folder_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            
            if parent_id:
                folder_metadata['parents'] = [parent_id]
            
            folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
            folder_id = folder.get('id')
            
            print(f"✅ Created folder: {folder_name} ({folder_id})")
            return folder_id
            
    except Exception as e:
        print(f"❌ Error with folder {folder_name}: {e}")
        return None

def upload_to_drive(content, filename, folder_id, mime_type='text/plain'):
    """Datei zu Google Drive hochladen"""
    try:
        if not drive_service or not folder_id:
            print("❌ Drive service or folder not available")
            return None
        
        # Content als BytesIO Stream
        file_stream = io.BytesIO(content.encode('utf-8'))
        
        # File Metadata
        file_metadata = {
            'name': filename,
            'parents': [folder_id]
        }
        
        # Media Upload
        media = MediaIoBaseUpload(file_stream, mimetype=mime_type, resumable=True)
        
        # Upload
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        file_id = file.get('id')
        print(f"✅ Uploaded to Drive: {filename} ({file_id})")
        
        return file_id
        
    except Exception as e:
        print(f"❌ Error uploading {filename}: {e}")
        return None

def clean_filename(title):
    """YouTube Titel für Dateiname bereinigen"""
    # Ungültige Zeichen entfernen
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        title = title.replace(char, '')
    
    # Zu lange Titel kürzen
    if len(title) > 100:
        title = title[:100]
    
    return title.strip()

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
                    summary TEXT,
                    drive_raw_file_id TEXT,
                    drive_summary_file_id TEXT
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS daily_summaries (
                    id INTEGER PRIMARY KEY,
                    date TEXT UNIQUE,
                    summary TEXT,
                    created_at TEXT,
                    drive_file_id TEXT
                )
            ''')
        
        print("✅ Database initialized successfully")
        
    except Exception as e:
        print(f"❌ Database initialization error: {e}")

def get_channel_videos(channel_id, hours_back=2):
    """Neue Videos eines Kanals abrufen"""
    try:
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
    """Transkript abrufen - API Version 0.6.2"""
    try:
        print(f"Getting transcript for video {video_id}")
        
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        
        try:
            transcript = transcript_list.find_transcript(['de'])
        except:
            try:
                transcript = transcript_list.find_transcript(['en'])
            except:
                transcript = transcript_list.find_generated_transcript(['de', 'en'])
        
        transcript_data = transcript.fetch()
        full_text = ' '.join([entry['text'] for entry in transcript_data])
        
        print(f"✅ Transcript fetched successfully, length: {len(full_text)} chars")
        return {
            'success': True,
            'transcript': full_text,
            'language': transcript.language_code
        }
    except Exception as e:
        print(f"❌ Transcript error for {video_id}: {e}")
        return {
            'success': False,
            'error': str(e)
        }

def call_gemini_api(prompt, model="gemini-1.5-flash"):
    """Gemini API für Zusammenfassungen"""
    try:
        print(f"Calling Gemini API with prompt length: {len(prompt)}")
        headers = {'Content-Type': 'application/json'}
        
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}'
        
        data = {
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {
                'maxOutputTokens': 4000,
                'temperature': 0.7
            }
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=60)
        response.raise_for_status()
        
        result = response.json()
        summary = result['candidates'][0]['content']['parts'][0]['text']
        print(f"✅ Gemini API response received, length: {len(summary)}")
        return summary
        
    except Exception as e:
        print(f"❌ Gemini API Error: {e}")
        return f"Fehler bei der Zusammenfassung: {e}"

def smart_chunk_processing(transcript):
    """Intelligente Chunk-Verarbeitung"""
    max_chunk_size = 30000
    
    if len(transcript) <= max_chunk_size:
        return call_gemini_api(INDIVIDUAL_VIDEO_PROMPT.format(transcript=transcript))
    
    print(f"Large transcript ({len(transcript)} chars), chunking...")
    
    sentences = transcript.split('. ')
    chunks = []
    current_chunk = ""
    
    for sentence in sentences:
        if len(current_chunk + sentence) > max_chunk_size:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = sentence
            else:
                chunks.append(sentence)
        else:
            current_chunk += sentence + ". "
    
    if current_chunk:
        chunks.append(current_chunk)
    
    print(f"Split into {len(chunks)} chunks")
    
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
    
    final_prompt = INDIVIDUAL_VIDEO_PROMPT.format(
        transcript='\n'.join([f"Teil {i+1}: {summary}" for i, summary in enumerate(chunk_summaries)])
    )
    
    return call_gemini_api(final_prompt)

def save_video_to_db_and_drive(video_data):
    """Video in DB UND Google Drive speichern"""
    with db_lock:
        try:
            # Dateinamen erstellen
            clean_title = clean_filename(video_data['title'])
            today = datetime.now().strftime('%Y-%m-%d')
            
            raw_filename = f"{clean_title}.txt"
            summary_filename = f"{clean_title}_{today}.txt"
            
            # Zu Google Drive hochladen
            raw_file_id = upload_to_drive(
                video_data['transcript'], 
                raw_filename, 
                DRIVE_FOLDER_IDS['raw']
            )
            
            summary_file_id = upload_to_drive(
                video_data['summary'], 
                summary_filename, 
                DRIVE_FOLDER_IDS['single']
            )
            
            # In Datenbank speichern
            with sqlite3.connect('youtube_monitor.db') as conn:
                conn.execute('''
                    INSERT OR REPLACE INTO videos 
                    (video_id, channel_name, title, published_at, processed_at, transcript, summary, drive_raw_file_id, drive_summary_file_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    video_data['video_id'],
                    video_data['channel_name'],
                    video_data['title'],
                    video_data['published_at'],
                    datetime.now().isoformat(),
                    video_data['transcript'],
                    video_data['summary'],
                    raw_file_id,
                    summary_file_id
                ))
                
            print(f"✅ Video saved to DB and Drive: {video_data['title']}")
            
        except Exception as e:
            print(f"❌ Error saving video: {e}")

def send_telegram_message(message):
    """Telegram Benachrichtigung senden"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Telegram not configured")
        return False
    
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
        data = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'Markdown'
        }
        
        response = requests.post(url, json=data, timeout=10)
        response.raise_for_status()
        
        print("✅ Telegram notification sent")
        return True
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return False

# DEBUG ENDPOINTS
@app.route('/debug/test_drive')
def debug_test_drive():
    """Google Drive Test"""
    try:
        if not drive_service:
            return jsonify({'error': 'Google Drive not initialized', 'configured': bool(GOOGLE_DRIVE_CREDENTIALS)})
        
        # Test Upload
        test_content = f"Test Upload - {datetime.now().isoformat()}"
        test_filename = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        
        file_id = upload_to_drive(test_content, test_filename, DRIVE_FOLDER_IDS['raw'])
        
        return jsonify({
            'status': 'ok',
            'drive_service': 'working',
            'folders': DRIVE_FOLDER_IDS,
            'test_upload': file_id is not None,
            'test_file_id': file_id
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
            'telegram': bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
            'google_drive': bool(GOOGLE_DRIVE_CREDENTIALS)
        },
        'storage': {
            'database': 'youtube_monitor.db',
            'google_drive': 'transkripte/ folder structure',
            'folders': DRIVE_FOLDER_IDS
        }
    })

@app.route('/monitor', methods=['POST'])
def monitor_videos():
    """YouTube Videos überwachen"""
    try:
        print("🔍 Starting video monitoring...")
        
        current_hour = datetime.utcnow().hour
        if current_hour < 7 or current_hour > 21:
            return jsonify({
                'message': 'Außerhalb der Monitoring-Zeiten (8-22 Uhr)',
                'current_utc_hour': current_hour
            })
        
        new_videos = []
        
        for youtube_key, youtube_data in YOUTUBERS.items():
            print(f"🔍 Checking channel: {youtube_data['name']}")
            videos = get_channel_videos(youtube_data['channel_id'])
            
            for video in videos:
                video_id = video['id']['videoId']
                title = video['snippet']['title']
                
                # Bereits verarbeitet?
                with sqlite3.connect('youtube_monitor.db') as conn:
                    cursor = conn.execute('SELECT id FROM videos WHERE video_id = ?', (video_id,))
                    if cursor.fetchone():
                        print(f"⏭️ Video already processed: {title}")
                        continue
                
                print(f"🎬 Processing new video: {title}")
                
                # Transkript abrufen
                transcript_result = get_transcript(video_id)
                if not transcript_result['success']:
                    print(f"❌ Could not get transcript: {transcript_result['error']}")
                    continue
                
                # Zusammenfassung erstellen
                summary = smart_chunk_processing(transcript_result['transcript'])
                
                # Video Daten
                video_data = {
                    'video_id': video_id,
                    'channel_name': youtube_data['name'],
                    'title': title,
                    'published_at': video['snippet']['publishedAt'],
                    'transcript': transcript_result['transcript'],
                    'summary': summary
                }
                
                # In DB und Drive speichern
                save_video_to_db_and_drive(video_data)
                new_videos.append(video_data)
                
                # Telegram Benachrichtigung
                telegram_message = f"""
🎬 **Neues Video verarbeitet!**

📺 **Channel:** {youtube_data['name']}
🎯 **Titel:** {title}
🕒 **Zeit:** {datetime.now().strftime('%H:%M')}
📁 **Google Drive:** ✅ Gespeichert

📝 **Zusammenfassung:**
{summary[:400]}{'...' if len(summary) > 400 else ''}

🔗 **Video:** https://youtube.com/watch?v={video_id}
                """
                
                send_telegram_message(telegram_message)
        
        print(f"✅ Monitoring completed. Processed {len(new_videos)} new videos.")
        
        return jsonify({
            'success': True,
            'new_videos': len(new_videos),
            'processed_videos': [{'channel': v['channel_name'], 'title': v['title']} for v in new_videos],
            'google_drive_uploads': len(new_videos) * 2,  # Raw + Summary
            'telegram_sent': len(new_videos) > 0
        })
        
    except Exception as e:
        print(f"❌ Error in monitor_videos: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/daily_summary', methods=['POST'])
def create_daily_summary():
    """Tägliche Zusammenfassung erstellen"""
    try:
        print("📊 Creating daily summary...")
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
            return jsonify({'message': 'Keine Videos heute verarbeitet'})
        
        # Zusammenfassung erstellen
        daily_content = ""
        for channel, title, summary in daily_videos:
            daily_content += f"\n**{channel} - {title}**\n{summary}\n\n"
        
        daily_summary = call_gemini_api(DAILY_SUMMARY_PROMPT.format(daily_content=daily_content))
        
        # Dateiname für Drive
        summary_filename = f"Tages_Zusammenfassung_{today}.txt"
        
        # Zu Google Drive hochladen
        drive_file_id = upload_to_drive(
            daily_summary, 
            summary_filename, 
            DRIVE_FOLDER_IDS['daily']
        )
        
        # In Datenbank speichern
        with sqlite3.connect('youtube_monitor.db') as conn:
            conn.execute('''
                INSERT OR REPLACE INTO daily_summaries (date, summary, created_at, drive_file_id)
                VALUES (?, ?, ?, ?)
            ''', (today, daily_summary, datetime.now().isoformat(), drive_file_id))
        
        # Telegram Benachrichtigung
        telegram_message = f"""
📊 **Tageszusammenfassung - {today}**
🎬 **Videos verarbeitet:** {len(daily_videos)}
📁 **Google Drive:** ✅ Gespeichert

{daily_summary[:800]}{'...' if len(daily_summary) > 800 else ''}

---
🤖 YouTube Monitor Bot
        """
        
        send_telegram_message(telegram_message)
        
        print(f"✅ Daily summary created for {len(daily_videos)} videos")
        
        return jsonify({
            'success': True,
            'videos_count': len(daily_videos),
            'summary': daily_summary[:500] + '...',
            'drive_file_id': drive_file_id,
            'telegram_sent': True
        })
        
    except Exception as e:
        print(f"❌ Error in daily_summary: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/videos')
def get_videos():
    """Alle verarbeiteten Videos anzeigen"""
    try:
        with sqlite3.connect('youtube_monitor.db') as conn:
            cursor = conn.execute('''
                SELECT video_id, channel_name, title, processed_at, summary, drive_raw_file_id, drive_summary_file_id
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
                    'youtube_url': f'https://www.youtube.com/watch?v={row[0]}',
                    'drive_raw_file': row[5],
                    'drive_summary_file': row[6]
                }
                for row in cursor.fetchall()
            ]
        
        return jsonify({
            'success': True,
            'count': len(videos),
            'videos': videos,
            'storage': 'Database + Google Drive'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/test_channel/<channel_key>')
def test_channel(channel_key):
    """Einzelnen Kanal testen"""
    if channel_key not in YOUTUBERS:
        return jsonify({'error': f'Channel {channel_key} not found'}), 404
    
    try:
        youtube_data = YOUTUBERS[channel_key]
        videos = get_channel_videos(youtube_data['channel_id'], hours_back=168)
        
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
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("🚀 Initializing YouTube Monitor...")
    init_db()
    
    # Google Drive initialisieren
    if init_google_drive():
        print("✅ Google Drive ready")
    else:
        print("❌ Google Drive not available")
    
    port = int(os.environ.get('PORT', 5000))
    print(f"🌐 Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

import os
import json
import csv
import sqlite3
import schedule
import time
import requests
from datetime import datetime, timedelta
from flask import Flask, jsonify
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from googleapiclient.http import MediaFileUpload
import google.generativeai as genai
import telegram
from telegram import Bot
from youtube_transcript_api import YouTubeTranscriptApi
import logging

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask App
app = Flask(__name__)

# Environment Variables
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
GOOGLE_SERVICE_ACCOUNT_KEY = os.getenv('GOOGLE_SERVICE_ACCOUNT_KEY')
GOOGLE_DRIVE_FOLDER_ID = os.getenv('GOOGLE_DRIVE_FOLDER_ID')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# YouTube Kanäle zum Überwachen (Demo-Kanäle für ersten Test)
CHANNELS_TO_MONITOR = [
    {
        'name': 'MrBeast',
        'channel_id': 'UCX6OQ3DkcsbYNE6H8uQQuVA',
        'keywords': ['challenge', 'money', 'give']
    },
    {
        'name': 'Veritasium',
        'channel_id': 'UCHnyfMqiRRG1u-2MsSQLbXA',
        'keywords': ['science', 'physics', 'experiment']
    }
]

# Globale Services
youtube_service = None
drive_service = None
telegram_bot = None

def initialize_services():
    """Initialisiere alle externen Services"""
    global youtube_service, drive_service, telegram_bot
    
    try:
        # YouTube API
        if YOUTUBE_API_KEY:
            youtube_service = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
            logger.info("✅ YouTube API initialisiert")
        
        # Google Drive API
        if GOOGLE_SERVICE_ACCOUNT_KEY:
            import base64
            service_account_info = json.loads(base64.b64decode(GOOGLE_SERVICE_ACCOUNT_KEY))
            credentials = Credentials.from_service_account_info(service_account_info)
            drive_service = build('drive', 'v3', credentials=credentials)
            logger.info("✅ Google Drive API initialisiert")
        
        # Telegram Bot
        if TELEGRAM_BOT_TOKEN:
            telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
            logger.info("✅ Telegram Bot initialisiert")
        
        # Gemini AI
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            logger.info("✅ Gemini AI initialisiert")
            
    except Exception as e:
        logger.error(f"❌ Fehler beim Initialisieren der Services: {e}")

def setup_database():
    """SQLite Datenbank für Video-Tracking"""
    conn = sqlite3.connect('youtube_monitor.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT UNIQUE,
            channel_name TEXT,
            title TEXT,
            published_at TEXT,
            description TEXT,
            view_count INTEGER,
            like_count INTEGER,
            transcript TEXT,
            summary TEXT,
            checked_at TEXT
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Datenbank initialisiert")

def get_channel_videos(channel_id, max_results=10):
    """Hole neueste Videos von einem YouTube-Kanal"""
    try:
        request = youtube_service.search().list(
            part='snippet',
            channelId=channel_id,
            maxResults=max_results,
            order='date',
            type='video',
            publishedAfter=(datetime.now() - timedelta(days=7)).isoformat() + 'Z'
        )
        
        response = request.execute()
        return response.get('items', [])
        
    except Exception as e:
        logger.error(f"❌ Fehler beim Abrufen der Videos: {e}")
        return []

def get_video_details(video_id):
    """Hole detaillierte Informationen zu einem Video"""
    try:
        request = youtube_service.videos().list(
            part='statistics,snippet',
            id=video_id
        )
        
        response = request.execute()
        if response['items']:
            return response['items'][0]
        return None
        
    except Exception as e:
        logger.error(f"❌ Fehler beim Abrufen der Video-Details: {e}")
        return None

def get_video_transcript(video_id):
    """Hole Transkript eines Videos"""
    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=['de', 'en'])
        text = ' '.join([entry['text'] for entry in transcript])
        return text
    except Exception as e:
        logger.warning(f"⚠️ Kein Transkript verfügbar für Video {video_id}: {e}")
        return None

def generate_summary(transcript):
    """Generiere Zusammenfassung mit Gemini AI"""
    try:
        if not GEMINI_API_KEY:
            return "Gemini AI nicht konfiguriert"
            
        model = genai.GenerativeModel('gemini-pro')
        prompt = f"""
        Erstelle eine kurze Zusammenfassung (max. 100 Wörter) des folgenden YouTube-Video-Transkripts:
        
        {transcript[:3000]}  # Beschränke auf erste 3000 Zeichen
        
        Fokussiere dich auf die wichtigsten Punkte und Erkenntnisse.
        """
        
        response = model.generate_content(prompt)
        return response.text
        
    except Exception as e:
        logger.error(f"❌ Fehler bei Gemini AI Zusammenfassung: {e}")
        return "Fehler bei der Zusammenfassung"

def save_to_database(video_data):
    """Speichere Video-Daten in Datenbank"""
    conn = sqlite3.connect('youtube_monitor.db')
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT OR REPLACE INTO videos 
            (video_id, channel_name, title, published_at, description, view_count, like_count, transcript, summary, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', video_data)
        
        conn.commit()
        logger.info(f"✅ Video gespeichert: {video_data[2]}")
        
    except Exception as e:
        logger.error(f"❌ Fehler beim Speichern in Datenbank: {e}")
    finally:
        conn.close()

def create_csv_report():
    """Erstelle CSV-Report aller Videos"""
    conn = sqlite3.connect('youtube_monitor.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM videos ORDER BY published_at DESC')
    videos = cursor.fetchall()
    
    filename = f'youtube_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    
    with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['ID', 'Video ID', 'Kanal', 'Titel', 'Veröffentlicht', 'Beschreibung', 
                        'Views', 'Likes', 'Transkript', 'Zusammenfassung', 'Überprüft'])
        writer.writerows(videos)
    
    conn.close()
    return filename

def upload_to_drive(filename):
    """Lade Datei zu Google Drive hoch"""
    try:
        if not drive_service:
            logger.error("❌ Google Drive Service nicht verfügbar")
            return None
            
        file_metadata = {
            'name': filename,
            'parents': [GOOGLE_DRIVE_FOLDER_ID]
        }
        
        media = MediaFileUpload(filename, resumable=True)
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        logger.info(f"✅ Datei zu Google Drive hochgeladen: {filename}")
        return file.get('id')
        
    except Exception as e:
        logger.error(f"❌ Fehler beim Upload zu Google Drive: {e}")
        return None

def send_telegram_notification(message):
    """Sende Telegram-Benachrichtigung"""
    try:
        if not telegram_bot or not TELEGRAM_CHAT_ID:
            logger.error("❌ Telegram Bot nicht konfiguriert")
            return False
            
        telegram_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode='HTML'
        )
        
        logger.info("✅ Telegram-Nachricht gesendet")
        return True
        
    except Exception as e:
        logger.error(f"❌ Fehler beim Senden der Telegram-Nachricht: {e}")
        return False

def check_channels():
    """Hauptfunktion: Überprüfe alle Kanäle"""
    logger.info("🔍 Starte Kanal-Überprüfung...")
    
    new_videos_count = 0
    all_new_videos = []
    
    for channel in CHANNELS_TO_MONITOR:
        logger.info(f"📺 Überprüfe Kanal: {channel['name']}")
        
        videos = get_channel_videos(channel['channel_id'])
        
        for video in videos:
            video_id = video['id']['videoId']
            
            # Prüfe ob Video bereits in Datenbank
            conn = sqlite3.connect('youtube_monitor.db')
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM videos WHERE video_id = ?', (video_id,))
            exists = cursor.fetchone()
            conn.close()
            
            if not exists:
                # Neues Video gefunden
                details = get_video_details(video_id)
                if details:
                    transcript = get_video_transcript(video_id)
                    summary = generate_summary(transcript) if transcript else "Kein Transkript verfügbar"
                    
                    video_data = (
                        video_id,
                        channel['name'],
                        details['snippet']['title'],
                        details['snippet']['publishedAt'],
                        details['snippet']['description'][:500],  # Beschränke Beschreibung
                        int(details['statistics'].get('viewCount', 0)),
                        int(details['statistics'].get('likeCount', 0)),
                        transcript,
                        summary,
                        datetime.now().isoformat()
                    )
                    
                    save_to_database(video_data)
                    all_new_videos.append({
                        'title': details['snippet']['title'],
                        'url': f"https://youtube.com/watch?v={video_id}",
                        'channel': channel['name'],
                        'published': details['snippet']['publishedAt']
                    })
                    new_videos_count += 1
    
    # Sende Benachrichtigung wenn neue Videos gefunden
    if new_videos_count > 0:
        message = f"🎬 <b>YouTube Monitor Update</b>\n\n"
        message += f"📊 Neue Videos gefunden: {new_videos_count}\n\n"
        
        for video in all_new_videos[:5]:  # Zeige max. 5 Videos
            message += f"📹 <b>{video['title'][:50]}...</b>\n"
            message += f"📺 Kanal: {video['channel']}\n"
            message += f"🔗 {video['url']}\n\n"
        
        # CSV-Report erstellen und hochladen
        csv_filename = create_csv_report()
        drive_file_id = upload_to_drive(csv_filename)
        
        if drive_file_id:
            message += "💾 CSV-Report in Google Drive hochgeladen"
        
        send_telegram_notification(message)
        
        # Lokale CSV-Datei löschen
        if os.path.exists(csv_filename):
            os.remove(csv_filename)
    
    logger.info(f"✅ Überprüfung abgeschlossen. {new_videos_count} neue Videos gefunden.")

# Flask-Routen für Render
@app.route('/')
def home():
    """Haupt-Endpoint"""
    return jsonify({
        "service": "YouTube Monitor",
        "status": "running",
        "timestamp": datetime.now().isoformat(),
        "endpoints": ["/health", "/status", "/manual-check"],
        "monitored_channels": len(CHANNELS_TO_MONITOR)
    })

@app.route('/health')
def health_check():
    """Health Check für Render"""
    try:
        env_vars = {
            "youtube": bool(YOUTUBE_API_KEY),
            "google_drive": bool(GOOGLE_SERVICE_ACCOUNT_KEY),
            "telegram": bool(TELEGRAM_BOT_TOKEN),
            "gemini": bool(GEMINI_API_KEY)
        }
        
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "api_keys_configured": env_vars,
            "monitored_channels": len(CHANNELS_TO_MONITOR),
            "database": "youtube_monitor.db"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/status')
def status():
    """Detaillierter Status"""
    try:
        # Prüfe letzte Überprüfung aus Datenbank
        conn = sqlite3.connect('youtube_monitor.db')
        cursor = conn.cursor()
        cursor.execute('SELECT MAX(checked_at) FROM videos')
        last_check = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM videos')
        total_videos = cursor.fetchone()[0]
        conn.close()
        
        return jsonify({
            "service": "YouTube Monitor",
            "status": "running",
            "environment": "production",
            "platform": "render.com",
            "timestamp": datetime.now().isoformat(),
            "last_check": last_check or "Noch keine Überprüfung",
            "total_videos_tracked": total_videos,
            "next_check": "Automatisch alle 2 Stunden"
        })
    except Exception as e:
        return jsonify({
            "status": "error", 
            "error": str(e)
        }), 500

@app.route('/manual-check')
def manual_check():
    """Manuelle Überprüfung starten"""
    try:
        check_channels()
        return jsonify({
            "status": "success",
            "message": "Manuelle Überprüfung erfolgreich gestartet",
            "timestamp": datetime.now().isoformat(),
            "note": "Überprüfe Telegram für Updates"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Fehler bei manueller Überprüfung: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/stats')
def stats():
    """Statistiken anzeigen"""
    try:
        conn = sqlite3.connect('youtube_monitor.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT channel_name, COUNT(*) FROM videos GROUP BY channel_name')
        channel_stats = cursor.fetchall()
        
        cursor.execute('SELECT COUNT(*) FROM videos WHERE checked_at > ?', 
                      [(datetime.now() - timedelta(days=1)).isoformat()])
        videos_last_24h = cursor.fetchone()[0]
        
        conn.close()
        
        return jsonify({
            "total_videos": sum([stat[1] for stat in channel_stats]),
            "videos_last_24h": videos_last_24h,
            "channel_stats": dict(channel_stats),
            "monitored_channels": len(CHANNELS_TO_MONITOR)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def start_scheduler():
    """Starte den Scheduler für automatische Überprüfungen"""
    schedule.every(2).hours.do(check_channels)
    
    while True:
        schedule.run_pending()
        time.sleep(60)  # Überprüfe jede Minute

# Hauptprogramm
if __name__ == '__main__':
    # Services initialisieren
    initialize_services()
    setup_database()
    
    # Erste Überprüfung beim Start
    logger.info("🚀 YouTube Monitor gestartet!")
    
    # Teste ob alle Services funktionieren
    try:
        check_channels()
    except Exception as e:
        logger.error(f"❌ Fehler beim ersten Check: {e}")
    
    # Flask-Server starten
    port = int(os.environ.get('PORT', 5000))
    
    # In Produktion: nur Flask-Server, kein Scheduler
    # (Render kann Cron-Jobs separat konfigurieren)
    app.run(host='0.0.0.0', port=port, debug=False)

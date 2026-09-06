import os
import sys
import shutil
import uuid
import base64
import json
import hashlib
import requests
import re
from typing import Optional, List
import asyncio

from fastapi import FastAPI, HTTPException, Form, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import edge_tts
from bs4 import BeautifulSoup
from google import genai

# =====================================================================
# 1. KONFIGURASI DIREKTORI & AI CLOUD (Render Ready)
# =====================================================================
BASE_DIR = os.path.abspath(".")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_DIR = os.path.join(BASE_DIR, "chroma_db")
ONLINE_AUDIO_DIR = os.path.join(BASE_DIR, "generated_audio")
MUSIK_DIR = os.path.join(BASE_DIR, "musik")

for folder in [UPLOAD_DIR, DB_DIR, ONLINE_AUDIO_DIR, MUSIK_DIR]:
    os.makedirs(folder, exist_ok=True)

app = FastAPI(title="KepoPDF Kids Cloud Backend", version="3.0.0")

# Middleware CORS agar Blogspot bisa mengakses backend Render
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Bisa diganti domain Blogspot Anda untuk keamanan
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inisialisasi Google Gemini API dari Environment Variable Render
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# =====================================================================
# 2. FUNGSI PEMBERSIH & TTS
# =====================================================================
def prepare_text_for_kids(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r'(?<![.?!])\n', ' ', text)
    cleaned = re.sub(r'\b(halaman|page)\s*\d+\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace('–', ', ').replace('—', ', ')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

class TTSRequest(BaseModel):
    text: str
    lang: Optional[str] = "id"

@app.post("/api/tts")
async def generate_online_tts(data: TTSRequest):
    text = prepare_text_for_kids(data.text)
    lang = data.lang or "id"
    if not text:
        raise HTTPException(status_code=400, detail="Teks masih kosong.")
    
    try:
        voice = "id-ID-GadisNeural" if lang.lower() in ["indonesia", "id"] else "en-US-AriaNeural"
        text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()[:12]
        audio_filename = f"edge_tts_{text_hash}.mp3"
        audio_path = os.path.join(ONLINE_AUDIO_DIR, audio_filename)

        if not os.path.exists(audio_path):
            communicate = edge_tts.Communicate(text, voice, rate="+0%", pitch="+2Hz")
            await communicate.save(audio_path)

        return FileResponse(audio_path, media_type="audio/mpeg")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =====================================================================
# 3. SCRAPER & INTEGRASI KHUSUS MONKEY PEN
# =====================================================================
@app.get("/api/monkeypen-books")
async def get_monkeypen_books():
    """Mengambil daftar buku cerita secara eksklusif dari Monkey Pen."""
    url = "https://monkeypen.com/pages/free-childrens-books"
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="Gagal terhubung ke situs Monkey Pen.")

        soup = BeautifulSoup(response.text, 'html.parser')
        books = []
        
        # Mencari tautan buku atau produk buku gratis di halaman Monkey Pen
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            title = a_tag.get_text(strip=True)
            if "products" in href and title and len(title) > 3:
                book_url = href if href.startswith("http") else f"https://monkeypen.com{href}"
                if {"title": title, "url": book_url} not in books:
                    books.append({"title": title, "url": book_url})

        return {"status": "success", "source": "https://monkeypen.com/pages/free-childrens-books", "books": books[:15]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Terjadi kesalahan saat memuat Monkey Pen: {str(e)}")

# =====================================================================
# 4. PABRIK PEMBUAT BUKU CERITA DENGAN GEMINI AI CLOUD
# =====================================================================
class StoryRequest(BaseModel):
    book_title: str
    target_lang: Optional[str] = "indonesia"

@app.post("/generate-storybook-cloud")
async def generate_storybook_cloud(data: StoryRequest):
    """Membuat narasi dongeng anak interaktif menggunakan Google Gemini AI Cloud."""
    if not gemini_client:
        raise HTTPException(status_code=500, detail="Gemini API Key belum disetel di environment variables Render.")

    try:
        prompt = (
            f"Buatkan adaptasi dongeng anak yang ramah, ceria, dan mendidik berdasarkan judul buku: '{data.book_title}' "
            f"dari sumber Monkey Pen. Bagi cerita menjadi 4 halaman/bagian terpisah yang menarik. "
            f"Berikan output dalam format JSON murni berupa list of object dengan struktur: "
            f"[{{\"page\": 1, \"text\": \"...\"}}, {{\"page\": 2, \"text\": \"...\"}}]."
        )
        
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        
        # Membersihkan respons teks agar menjadi JSON valid
        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:-3].strip()
        elif raw_text.startswith("```"):
            raw_text = raw_text[3:-3].strip()

        pages_data = json.loads(raw_text)
        
        # Generate Audio TTS untuk setiap halaman
        voice = "id-ID-GadisNeural" if data.target_lang.lower() in ["indonesia", "id"] else "en-US-AriaNeural"
        final_pages = []

        for item in pages_data:
            page_num = item.get("page", 1)
            text_content = item.get("text", "")
            
            clean_t = prepare_text_for_kids(text_content)
            text_hash = hashlib.md5(clean_t.encode('utf-8')).hexdigest()[:12]
            audio_name = f"monkey_edge_{text_hash}_p{page_num}.mp3"
            audio_path = os.path.join(ONLINE_AUDIO_DIR, audio_name)

            if not os.path.exists(audio_path):
                communicate = edge_tts.Communicate(clean_t, voice, rate="+0%", pitch="+2Hz")
                await communicate.save(audio_path)

            final_pages.append({
                "page": page_num,
                "text": text_content,
                "audio_url": f"/audio-online/{audio_name}"
            })

        return {
            "status": "success",
            "book_title": data.book_title,
            "pages": final_pages
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal meracik cerita AI: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
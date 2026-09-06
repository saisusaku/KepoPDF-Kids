import os
import sys
import shutil
import uuid
import base64
import json
import hashlib
import requests
import re
from typing import Optional, List, Union
import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import edge_tts
from groq import Groq
import fitz  # PyMuPDF untuk render cover halaman pertama PDF

# =====================================================================
# 1. KONFIGURASI DIREKTORI & GROQ AI
# =====================================================================
BASE_DIR = os.path.abspath(".")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_DIR = os.path.join(BASE_DIR, "chroma_db")
ONLINE_AUDIO_DIR = os.path.join(BASE_DIR, "generated_audio")
THUMB_DIR = os.path.join(BASE_DIR, "thumbnails")
MUSIK_DIR = os.path.join(BASE_DIR, "musik")
DOKUMEN_DIR = os.path.join(BASE_DIR, "dokumen")

for folder in [UPLOAD_DIR, DB_DIR, ONLINE_AUDIO_DIR, THUMB_DIR, MUSIK_DIR, DOKUMEN_DIR]:
    os.makedirs(folder, exist_ok=True)

app = FastAPI(title="KepoPDF Kids Cloud Backend", version="4.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/audio-online", StaticFiles(directory=ONLINE_AUDIO_DIR), name="audio-online")
app.mount("/dokumen", StaticFiles(directory=DOKUMEN_DIR), name="dokumen")
app.mount("/thumbnails", StaticFiles(directory=THUMB_DIR), name="thumbnails")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# =====================================================================
# 2. FUNGSI PEMBERSIH & TTS (EDGE-TTS)
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
# 3. PEMBACA FOLDER DOKUMEN (KATALOG, URL, & THUMBNAIL HALAMAN PERTAMA)
# =====================================================================
@app.get("/api/monkeypen-books")
async def get_local_documents():
    try:
        if not os.path.exists(DOKUMEN_DIR):
            return {"status": "success", "source": "local_folder", "books": []}

        files = sorted(os.listdir(DOKUMEN_DIR))
        books = []
        
        for filename in files:
            if filename.lower().endswith(('.pdf', '.txt', '.epub', '.docx')):
                clean_title = os.path.splitext(filename)[0].replace('_', ' ').replace('-', ' ').title()
                file_path = os.path.join(DOKUMEN_DIR, filename)
                
                # Buat thumbnail otomatis jika formatnya PDF
                cover_url = ""
                if filename.lower().endswith('.pdf'):
                    thumb_filename = f"{os.path.splitext(filename)[0]}_thumb.png"
                    thumb_path = os.path.join(THUMB_DIR, thumb_filename)
                    
                    if not os.path.exists(thumb_path):
                        try:
                            doc = fitz.open(file_path)
                            if len(doc) > 0:
                                page = doc[0]
                                pix = page.get_pixmap(dpi=100)
                                pix.save(thumb_path)
                        except Exception as e:
                            print(f"Gagal membuat thumbnail {filename}: {e}")
                    
                    if os.path.exists(thumb_path):
                        cover_url = f"/thumbnails/{thumb_filename}"

                preview_snippet = "Buku bersumber resmi siap dibaca oleh AI!"
                if filename.lower().endswith('.txt'):
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f_txt:
                            preview_snippet = f_txt.read(300).strip()
                    except:
                        pass

                books.append({
                    "title": clean_title,
                    "filename": filename,
                    "url": f"/dokumen/{filename}",
                    "pdf_url": f"/dokumen/{filename}",  # Kompatibilitas frontend
                    "cover_url": cover_url,
                    "preview": preview_snippet
                })

        return {"status": "success", "source": "local_folder", "books": books}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Terjadi kesalahan saat membaca folder dokumen: {str(e)}")

# =====================================================================
# 4. PABRIK PEMBACA & PENERJEMAH ISI DOKUMEN ASLI DENGAN GROQ AI
# =====================================================================
@app.post("/generate-storybook-cloud")
async def generate_storybook_cloud(request: Request):
    if not groq_client:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY belum disetel di environment variables.")

    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else await request.form()
        book_title = body.get("book_title") if isinstance(body, dict) else body.get("book_title", "Unknown")
        target_lang = body.get("target_lang", "indonesia") if isinstance(body, dict) else "indonesia"

        file_content_context = ""
        safe_title_slug = str(book_title).lower().replace(" ", "_")
        
        if os.path.exists(DOKUMEN_DIR):
            for f in os.listdir(DOKUMEN_DIR):
                if safe_title_slug in f.lower() or f.lower().startswith(safe_title_slug[:5]):
                    file_path = os.path.join(DOKUMEN_DIR, f)
                    if f.endswith('.txt'):
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as file_obj:
                            file_content_context = file_obj.read()
                    elif f.endswith('.pdf'):
                        try:
                            doc = fitz.open(file_path)
                            extracted_text = []
                            for page in doc:
                                extracted_text.append(page.get_text())
                            file_content_context = "\n".join(extracted_text)
                        except Exception as e:
                            print(f"Gagal ekstrak teks PDF: {e}")
                    break

        if not file_content_context and os.path.exists(DOKUMEN_DIR):
            files = [f for f in os.listdir(DOKUMEN_DIR) if f.endswith(('.txt', '.pdf'))]
            if files:
                f_path = os.path.join(DOKUMEN_DIR, files[0])
                if files[0].endswith('.pdf'):
                    doc = fitz.open(f_path)
                    file_content_context = "\n".join([p.get_text() for p in doc])
                else:
                    with open(f_path, 'r', encoding='utf-8', errors='ignore') as file_obj:
                        file_content_context = file_obj.read()

        if not file_content_context:
            file_content_context = f"Judul Buku: {book_title}."

        json_format_example = '[{"page": 1, "text": "..."}, {"page": 2, "text": "..."}, {"page": 3, "text": "..."}, {"page": 4, "text": "..."}]'
        
        prompt = (
            f"Anda adalah asisten pembaca buku anak profesional. Tugas Anda adalah membaca ISI TEKS ASLI dari dokumen di bawah ini, "
            f"lalu menyusunnya kembali menjadi alur cerita anak yang terbagi menjadi 4 halaman/bagian secara berurutan. "
            f"PENTING: Jangan mengarang cerita di luar isi dokumen. Terjemahkan ke dalam Bahasa Indonesia yang ramah anak jika isi aslinya berbahasa Inggris.\n\n"
            f"--- ISI DOKUMEN ASLI ---\n{file_content_context[:8000]}\n------------------------\n\n"
            f"Berikan output HANYA dalam format JSON murni berupa list of object dengan struktur persis seperti ini: {json_format_example} "
            f"Jangan sertakan teks pengantar atau penutup lain di luar format JSON."
        )
        
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        
        raw_text = completion.choices[0].message.content.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:-3].strip()
        elif raw_text.startswith("```"):
            raw_text = raw_text[3:-3].strip()

        pages_data = json.loads(raw_text)
        
        voice = "id-ID-GadisNeural" if str(target_lang).lower() in ["indonesia", "id"] else "en-US-AriaNeural"
        final_pages = []

        for item in pages_data:
            page_num = item.get("page", 1)
            text_content = item.get("text", "")
            
            clean_t = prepare_text_for_kids(text_content)
            text_hash = hashlib.md5(clean_t.encode('utf-8')).hexdigest()[:12]
            audio_name = f"doc_edge_{text_hash}_p{page_num}.mp3"
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
            "book_title": book_title,
            "pages": final_pages
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": f"Gagal membaca dokumen asli: {str(e)}"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

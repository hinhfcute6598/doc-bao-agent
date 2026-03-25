import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import sys
import io
import google.generativeai as genai
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

# Đảm bảo in được tiếng Việt trên Terminal Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# --- CẤU HÌNH ---
# Lấy Token từ biến môi trường để đảm bảo bảo mật khi đưa lên GitHub
TOKEN = os.environ.get("TELEGRAM_TOKEN", "7759991714:AAFMP56X2u8ZtasssI9CQgr3mEiHqTf4DQY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyAJCrTElt-6_QHPFviSpQQlJc6nS2yRYug")

# Cấu hình Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Danh sách nguồn tin (Đã mở rộng)
NEWS_SOURCES = {
    "vnexpress_gocnhin": {"name": "VnExpress - Góc nhìn", "url": "https://vnexpress.net/rss/goc-nhin.rss"},
    "vnexpress_kinhdoanh": {"name": "VnExpress - Kinh doanh", "url": "https://vnexpress.net/rss/kinh-doanh.rss"},
    "vnexpress_tech": {"name": "VnExpress - Số hóa", "url": "https://vnexpress.net/rss/so-hoa.rss"},
}

ARTICLE_LIMIT = 5  # Đã tăng lên 5 bài mỗi lần quét

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- LOGIC LẤY TIN VÀ TÓM TẮT ĐÃ ĐƯỢC TỐI ƯU VÀO HANDLER ĐỂ CÓ TRẢI NGHIỆM REAL-TIME ---

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    
    # Tạo Menu tự động (2 cột để tiết kiệm không gian)
    keyboard = []
    keys = list(NEWS_SOURCES.keys())
    for i in range(0, len(keys), 2):
        row = [InlineKeyboardButton(NEWS_SOURCES[k]['name'], callback_data=k) for k in keys[i:i+2]]
        keyboard.append(row)
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Chào anh {user_name}! Em là Nodal Kuiper Agent.\n"
        "Anh muốn đọc báo nhanh chuyên mục nào sáng nay?",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    source_key = query.data
    source_name = NEWS_SOURCES[source_key]['name']
    status_msg = await query.edit_message_text(text=f"🚀 Bắt đầu quét tin từ {source_name}...\nEm sẽ gửi từng bài ngay khi tóm tắt xong nhé!")
    
    # Lấy danh sách RSS (Nhanh)
    source = NEWS_SOURCES.get(source_key)
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(connect=3, backoff_factor=1)))
    
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    
    try:
        response = session.get(source['url'], headers=headers, timeout=15)
        soup = BeautifulSoup(response.content, 'xml')
        items = soup.find_all('item')[:ARTICLE_LIMIT]
        
        if not items:
            await query.message.reply_text("Hiện chưa có tin mới trong chuyên mục này ạ.")
            return


        # --- CHẠY TUẦN TỰ ĐỂ GỠ LỖI (DEBUG MODE) ---
        for i, item in enumerate(items):
            title = item.title.text.strip()
            link = item.link.text.strip()
            desc_raw = item.description.text if item.description else ""
            
            await status_msg.edit_text(f"🔍 Đang gỡ lỗi bài {i+1}/{len(items)}:\n{title}")
            
            # 1. Thử lấy nội dung chi tiết
            full_text = ""
            try:
                logging.info(f"Đang thử kết nối tới link: {link}")
                art_res = requests.get(link, headers=headers, timeout=10)
                logging.info(f"Kết quả HTTP: {art_res.status_code}, Độ dài trang: {len(art_res.content)}")
                
                art_soup = BeautifulSoup(art_res.content, 'html.parser')
                # Loại bỏ rác
                for s in art_soup(['script', 'style', 'header', 'footer', 'nav', 'aside']): s.extract()
                
                # Cách lấy nội dung linh hoạt hơn: Lấy text từ các thẻ div/p có nhiều chữ
                paragraphs = art_soup.find_all(['p', 'div'], recursive=True)
                content_parts = []
                for p in paragraphs:
                    txt = p.get_text().strip()
                    if len(txt) > 60: # Chỉ lấy các đoạn có độ dài đáng kể
                        content_parts.append(txt)
                
                full_text = "\n".join(content_parts[:20]) # Lấy tối đa 20 đoạn đầu
                logging.info(f"Đã trích xuất được {len(full_text)} ký tự văn bản.")
                
            except Exception as e:
                logging.error(f"Lỗi khi cào trang gốc: {e}")
            
            # Nếu cào thất bại hoặc nội dung quá ngắn, dùng description từ RSS
            if len(full_text) < 200:
                logging.info("Nội dung chi tiết quá ngắn, chuyển sang dùng Description từ RSS.")
                desc_soup = BeautifulSoup(desc_raw, 'html.parser')
                full_text = desc_soup.get_text().strip()

            # 2. Gửi sang AI tóm tắt (Hybrid Prompt)
            prompt = (
                f"Bạn là chuyên gia phân tích tin tức cao cấp cho anh Hình (Marketing Expert).\n"
                f"Tóm tắt bài báo theo cấu trúc LAI (Hỏi đáp + Kết luận + Dẫn chứng):\n\n"
                f"TIÊU ĐỀ: {title}\n"
                f"NỘI DUNG: {full_text[:3500]}\n\n"
                f"YÊU CẦU:\n"
                f"1. PHẦN HỎI ĐÁP: Tiêu đề là câu hỏi, hãy giải đáp trực diện.\n"
                f"2. KẾT LUẬN & DẪN CHỨNG: Đưa ra 1 kết luận chính và các số liệu chứng minh cụ thể.\n"
                f"3. CƠ HỘI: Lời khuyên thực chiến cho anh Hình.\n"
                f"Trả lời bằng tiếng Việt."
            )
            
            try:
                ai_response = model.generate_content(prompt)
                highlights = ai_response.text
            except Exception as ai_err:
                logging.error(f"Lỗi AI: {ai_err}")
                highlights = f"• Lỗi tóm tắt AI: {ai_err}\n\nNội dung thô: {full_text[:300]}"

            # 3. Gửi tin nhắn ngay
            message = (
                f"🗞 *{title}*\n\n"
                f"💡 *Phân tích chi tiết:*\n"
                f"{highlights}\n\n"
                f"🔗 [Đọc bài gốc]({link})\n"
                f"-------------------"
            )
            await query.message.reply_text(text=message, parse_mode='Markdown')
            
        await status_msg.edit_text(f"✅ Hoàn tất gỡ lỗi {len(items)} bài từ {source_name}.")

    except Exception as e:
        logging.error(f"Lỗi: {e}")
        await query.message.reply_text(f"Lỗi: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    # Mở rộng bộ lọc từ khóa theo thói quen của anh Hình
    if any(word in text for word in ["chào", "đọc báo", "tin tức", "hi", "hello", "tiếp", "tin"]):
        await start(update, context)

# --- RENDER HEALTH CHECK SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_health_check():
    port = int(os.environ.get("PORT", 8080))
    server_address = ('', port)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    print(f"Health check server running on port {port}...")
    httpd.serve_forever()

if __name__ == '__main__':
    # Chạy Health Check Server trong một luồng riêng để không làm gián đoạn Bot
    threading.Thread(target=run_health_check, daemon=True).start()
    
    application = ApplicationBuilder().token(TOKEN).build()
    
    start_handler = CommandHandler('start', start)
    button_callback_handler = CallbackQueryHandler(button_handler)
    text_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text)
    
    application.add_handler(start_handler)
    application.add_handler(button_callback_handler)
    application.add_handler(text_handler)
    
    print("Bot đang chạy... Anh Hình hãy vào Telegram và nhấn Start nhé!")
    
    # Thêm cơ chế tự hồi sinh nếu gặp lỗi mạng hoặc xung đột
    while True:
        try:
            print("Hệ thống Polling bắt đầu hoạt động...")
            application.run_polling(drop_pending_updates=True)
        except Exception as e:
            logging.error(f"Bot gặp sự cố và đang tự khởi động lại: {e}")
            time.sleep(5) # Đợi 5 giây rồi thử lại (Dùng time.sleep chuẩn cho block __main__)

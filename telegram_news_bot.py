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
    "brands_vn": {"name": "Brands Vietnam", "url": "https://www.brandsvietnam.com/rss"},
    "vneconomy_kinhteso": {"name": "VnEconomy - Kinh tế số", "url": "https://vneconomy.vn/rss/kinh-te-so.htm"},
    "vneconomy_chungkhoan": {"name": "VnEconomy - Chứng khoán", "url": "https://vneconomy.vn/rss/chung-khoan.htm"},
    "vneconomy_thitruong": {"name": "VnEconomy - Thị trường", "url": "https://vneconomy.vn/rss/thi-truong.htm"}
}

ARTICLE_LIMIT = 5  # Đã tăng lên 5 bài mỗi lần quét

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- LOGIC LẤY TIN VỚI CƠ CHẾ THỬ LẠI (RETRY) ---
def fetch_news(source_key):
    source = NEWS_SOURCES.get(source_key)
    if not source: return "Không tìm thấy nguồn tin."
    
    # Thiết lập cơ chế thử lại 3 lần nếu lỗi kết nối
    session = requests.Session()
    retry = Retry(connect=3, backoff_factor=1)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    session.mount('http://', adapter)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    }
    
    try:
        logging.info(f"--- BẮT ĐẦU QUÉT TIN: {source_name} ---")
        # Tăng timeout lên 20 giây để bù đắp độ trễ mạng
        response = session.get(source['url'], headers=headers, timeout=20)
        soup = BeautifulSoup(response.content, 'xml')
        items = soup.find_all('item')[:ARTICLE_LIMIT] 
        
        logging.info(f"Tìm thấy {len(items)} bài mới. Đang bắt đầu tóm tắt bằng Gemini AI...")
        
        results = []
        for item in items:
            title = item.title.text.strip()
            link = item.link.text.strip()
            desc_raw = item.description.text if item.description else ""
            
            # --- LẤY NỘI DUNG CHI TIẾT TỪ TRANG GỐC (Fix lỗi thiếu nội dung) ---
            full_text = ""
            try:
                # Thử lấy nội dung từ link gốc để AI có đủ dữ liệu tóm tắt
                art_res = session.get(link, headers=headers, timeout=10)
                art_soup = BeautifulSoup(art_res.content, 'html.parser')
                
                # Loại bỏ các thành phần rác
                for s in art_soup(['script', 'style', 'header', 'footer', 'nav']): s.extract()
                
                # Lấy các đoạn văn bản chính (Thường nằm trong thẻ p)
                paragraphs = art_soup.find_all('p')
                full_text = "\n".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 30])
                
                # Nếu không lấy được nội dung chi tiết, dùng tạm description từ RSS
                if len(full_text) < 200:
                    desc_soup = BeautifulSoup(desc_raw, 'html.parser')
                    full_text = desc_soup.get_text().strip()
            except:
                desc_soup = BeautifulSoup(desc_raw, 'html.parser')
                full_text = desc_soup.get_text().strip()
            
            # --- TÓM TẮT THÔNG MINH LAI (PHƯƠNG ÁN 1 + 2) ---
            prompt = (
                f"Bạn là chuyên gia phân tích tin tức cao cấp cho anh Hình (Marketing Expert).\n"
                f"Hãy tóm tắt bài báo sau theo cấu trúc LAI tối ưu:\n\n"
                f"TIÊU ĐỀ: {title}\n"
                f"NỘI DUNG: {full_text[:3000]}\n\n" # Giới hạn 3000 ký tự để tránh quá tải AI
                f"YÊU CẦU CẤU TRÚC:\n"
                f"1. PHẦN HỎI ĐÁP (Q&A): Coi Tiêu đề là một câu hỏi. Hãy đưa ra câu trả lời trực diện và lý do tại sao tin này quan trọng.\n"
                f"2. KẾT LUẬN CHÍNH: Rút ra thông điệp cốt lõi nhất của bài báo trong 1-2 câu.\n"
                f"3. DẪN CHỨNG CHỨNG MINH: Liệt kê 3-4 luận điểm kèm số liệu/sự kiện cụ thể trong bài để chứng minh cho kết luận trên.\n"
                f"4. CƠ HỘI MARKETING: Lời khuyên thực chiến cho anh Hình.\n\n"
                f"PHONG CÁCH: Sắc bén, chuyên nghiệp, ngôn ngữ thực chiến. Trả lời bằng tiếng Việt."
            )
            
            try:
                ai_response = model.generate_content(prompt)
                highlights = ai_response.text
            except:
                # Nếu AI lỗi thì dùng cách cũ (fallback)
                sentences = [s.strip() for s in full_text.split('.') if len(s.strip()) > 10]
                highlights = "\n".join([f"• {s}" for s in sentences[:3]])
            
            results.append({
                "title": title,
                "link": link,
                "highlights": highlights
            })
        logging.info(f"--- TỔNG HỢP XONG: {source_name} ---")
        return results
    except Exception as e:
        logging.error(f"Lỗi trong quá trình lấy tin: {e}")
        return f"Lỗi khi lấy tin từ {source_name}: {e}"

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
    await query.edit_message_text(text=f"Đang quét tin từ {source_name}...\n(Quá trình tóm tắt AI có thể mất 15-20 giây, anh đợi em chút nhé!)")
    
    # CHẠY TÁC VỤ NẶNG TRONG LUỒNG RIÊNG (Không gây nghẽn Bot)
    loop = asyncio.get_event_loop()
    news_list = await loop.run_in_executor(None, fetch_news, source_key)
    
    if isinstance(news_list, str):
        await query.message.reply_text(news_list)
        return

    for art in news_list:
        # Định dạng HIGHLIGHT cho anh Hình (Lướt 1 phút)
        message = (
            f"🗞 *{art['title']}*\n\n"
            f"💡 *Ý chính:*\n"
            f"{art['highlights']}\n\n"
            f"🔗 [Đọc bài viết đầy đủ]({art['link']})\n"
            f"-------------------"
        )
        await query.message.reply_text(text=message, parse_mode='Markdown')

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

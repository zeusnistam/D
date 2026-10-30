import os
import time
import telebot
import yt_dlp

# ==================== تنظیمات ربات ====================
BOT_TOKEN = "7751568756:AAHES0_R7QnMS5oMEkzf_qb_5Xe6WLOYlBs"
# ======================================================

bot = telebot.TeleBot(BOT_TOKEN)

# آدرس پوشه دانلودهای گوشی (مسیر دقیق حافظه داخلی)
DOWNLOAD_DIR = "/data/data/com.termux/files/home/storage/shared/Download"

# اگر پوشه وجود نداشت، بسازش
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        f"سلام {message.from_user.first_name} عزیز! 👋\n\n"
        "این ربات بدون هیچ کتابخانه سنگینی کار می‌کند.\n"
        "لینک ویدیوی مورد نظر خود را بفرست تا نسخه کم‌حجم آن را دانلود کرده و هم در گوشی ذخیره کنم و هم برایت بفرستم:"
    )
    bot.reply_to(message, welcome_text)


@bot.message_handler(func=lambda message: True)
def handle_video_link(message):
    url = message.text.strip()
    
    if not url.startswith("http"):
        bot.reply_to(message, "⚠️ لطفاً یک لینک معتبر (شروع شده با http یا https) ارسال کنید.")
        return

    status_msg = bot.reply_to(message, "⏳ در حال پردازش و دانلود نسخه کم‌حجم ویدیو...")
    
    file_path = None
    try:
        # تنظیمات yt-dlp برای ذخیره مستقیم در پوشه Download گوشی با کیفیت مناسب و کم‌حجم
        ydl_opts = {
            'format': 'best[height<=360]/best[height<=480]/worst',
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'noplaylist': True,
        }

        # دانلود ویدیو
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        if not file_path or not os.path.exists(file_path):
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text="❌ دانلود ویدیو ناموفق بود یا لینک معتبر نیست."
            )
            return

        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            text="📤 در حال آپلود ویدیو در تلگرام..."
        )

        # ارسال فایل ویدیو به کاربر
        with open(file_path, 'rb') as video_file:
            bot.send_video(message.chat.id, video_file, caption="✅ ویدیوی درخواستی شما (هم در تلگرام ارسال شد و هم در پوشه Download گوشی ذخیره گردید)")

        # پاک کردن پیام وضعیت
        bot.delete_message(message.chat.id, status_msg.message_id)

    except Exception as e:
        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            text=f"❌ خطایی رخ داد: {str(e)}"
        )
    # توجه: اینجا فایل رو از پوشه دانلودها پاک نمی‌کنیم تا توی گوشیِ خودت ذخیره بمونه!


# اجرای ربات به صورت مداوم
if __name__ == "__main__":
    print("ربات بدون aiogram و بدون باگ روشن شد...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"خطای ارتباطی: {e}, تلاش مجدد در ۵ ثانیه...")
            time.sleep(5)

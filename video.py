import asyncio
import logging
import os
import sys
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp

# ==================== تنظیمات ربات ====================
BOT_TOKEN = "7751568756:AAHES0_R7QnMS5oMEkzf_qb_5Xe6WLOYlBs"
 #======================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ایجاد پوشه دانلودها اگر وجود نداشته باشد
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

class DownloadStates(StatesGroup):
    waiting_for_url = State()

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """منوی شروع"""
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 دانلود ویدیو (کم‌حجم)", callback_data="start_download")
    builder.button(text="ℹ️ راهنما", callback_data="help")
    builder.adjust(1)
    
    await message.answer(
        f"سلام **{message.from_user.first_name}** عزیز! 👋\n\n"
        "این ربات ویدیوها را با **کمترین حجم** دانلود کرده و مستقیم برایتان ارسال می‌کند.\n"
        "روی دکمه‌ی زیر کلیک کنید:",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "help")
async def callback_help(callback: types.CallbackQuery):
    await callback.message.answer(
        "💡 **راهنما:**\n\n"
        "۱. روی دکمه دانلود کلیک کنید.\n"
        "۲. لینک ویدیوی مورد نظر (یا لینک سرچ/صفحه ویدیو) را بفرستید.\n"
        "۳. ربات نسخه کم‌حجم آن را دانلود کرده و ارسال می‌کند.",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "start_download")
async def callback_start_download(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("لطفاً **لینک ویدیوی** مورد نظر خود را ارسال کنید:")
    await state.set_state(DownloadStates.waiting_for_url)
    await callback.answer()


@dp.message(DownloadStates.waiting_for_url)
async def process_video_url(message: types.Message, state: FSMContext):
    url = message.text.strip()
    await state.clear()
    
    status_msg = await message.answer("⏳ در حال پردازش و دانلود نسخه کم‌حجم ویدیو...")
    
    file_path = None
    try:
        # تنظیمات yt-dlp برای دانلود کم‌حجم‌ترین کیفیت ممکن (ترجیحاً 360p یا پایین‌تر)
        ydl_opts = {
            'format': 'best[height<=360]/best[height<=480]/worst',  # انتخاب کیفیت پایین برای حجم کم
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'noplaylist': True,
        }

        # اجرای دانلود در پس‌زمینه (برای جلوگیری از فریز شدن ربات)
        def download_video():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info)

        file_path = await asyncio.to_thread(download_video)

        if not file_path or not os.path.exists(file_path):
            await bot.edit_message_text(
                text="❌ دانلود ویدیو ناموفق بود یا لینک معتبر نیست.",
                chat_id=message.chat.id,
                message_id=status_msg.message_id
            )
            return

        await bot.edit_message_text(
            text="📤 در حال آپلود ویدیو در تلگرام...",
            chat_id=message.chat.id,
            message_id=status_msg.message_id
        )

        # ارسال فایل ویدیو به کاربر
        video_file = types.FSInputFile(file_path)
        await message.answer_video(video=video_file, caption="✅ ویدیوی درخواستی شما (کم‌حجم)")

        # پاک کردن پیام وضعیت قبلی
        await bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)

    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        await bot.edit_message_text(
            text=f"❌ خطایی رخ داد: `{str(e)}`",
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            parse_mode="Markdown"
        )
    finally:
        # پاکسازی فایل از روی سرور/گوشی پس از ارسال (مدیریت فضای ذخیره‌سازی)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Error removing file: {e}")


async def main():
    logger.info("ربات دانلودر روشن شد...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("ربات متوقف شد.")

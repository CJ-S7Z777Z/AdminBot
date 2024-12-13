import logging
import os
import tempfile
import sqlite3
import json
import uuid
import re
from functools import wraps
from dotenv import load_dotenv
import redis
import asyncio
from PIL import Image


if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.Resampling.LANCZOS

from telegram import (
    Bot, Update, InputMediaPhoto, InputMediaVideo, InputMediaAudio,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler
)
from moviepy.editor import VideoFileClip


load_dotenv()


logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


(
    ADMIN_PANEL,
    SEND_POST_CHOICES,
    SEND_POST_TEXT,
    SPOILER_DECISION_TEXT,
    SPOILER_PART_DECISION_TEXT,
    HIDE_TEXT_FRAGMENT,
    SEND_POST_MEDIA,
    SPOILER_DECISION_MEDIA,
    SEND_POST_AUDIO,
    SEND_VIDEO_NOTE,
    SEND_BOTH_TEXT,
    SEND_BOTH_SPOILER_DECISION_TEXT,
    SEND_BOTH_SPOILER_PART_DECISION_TEXT,
    SEND_BOTH_HIDDEN_FRAGMENT,
    SEND_BOTH_MEDIA,
    SEND_BOTH_AUDIO,
    SELECT_POST,
    EDIT_POST,
    DELETE_POST,
    SELECT_BOT_VIDEO_AUDIO,  
    SELECT_BOT_POST,        
    SELECT_BOT              
) = range(22)


ADMIN_BOT_TOKEN = os.getenv('ADMIN_BOT_TOKEN')
ALLOWED_USER_IDS = os.getenv('ALLOWED_USER_IDS', '')
ALLOWED_USER_IDS = [int(uid.strip()) for uid in ALLOWED_USER_IDS.split(',') if uid.strip().isdigit()]

class SendingBotManager:
    def __init__(self, name, config):
        self.name = name 
        self.bot_token = config['BOT_TOKEN']
        self.redis_host = config['REDIS_HOST']
        self.redis_port = int(config['REDIS_PORT'])
        self.redis_username = config['REDIS_USERNAME']
        self.redis_password = config['REDIS_PASSWORD']
        self.redis_db = int(config['REDIS_DB'])
        self.chat_id_set = config['CHAT_ID_COLUMN']
        self.db_path = config['DB_PATH']
        self.bot_name = self.name 


        self.redis_client = redis.Redis(
            host=self.redis_host,
            port=self.redis_port,
            username=self.redis_username,
            password=self.redis_password,
            db=self.redis_db,
            decode_responses=True
        )


        self.bot = Bot(token=self.bot_token)


        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS posts (
                post_id TEXT PRIMARY KEY,
                content TEXT,
                post_type TEXT,
                data TEXT,
                bot_name TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT,
                chat_id INTEGER,
                message_id INTEGER,
                bot_name TEXT
            )
        ''')
        conn.commit()
        conn.close()

    def save_post(self, post_id, content, post_type, data, bot_name):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO posts (post_id, content, post_type, data, bot_name) 
            VALUES (?, ?, ?, ?, ?)
        ''', (post_id, content, post_type, data, bot_name))
        conn.commit()
        conn.close()

    def get_post(self, post_id, bot_name):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT content, post_type, data FROM posts 
            WHERE post_id=? AND bot_name=?
        ''', (post_id, bot_name))
        result = cursor.fetchone()
        conn.close()
        return result

    def delete_post(self, post_id, bot_name):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM posts WHERE post_id=? AND bot_name=?
        ''', (post_id, bot_name))
        conn.commit()
        conn.close()

    def add_sent_message(self, post_id, chat_id, message_id, bot_name):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO sent_messages (post_id, chat_id, message_id, bot_name) 
            VALUES (?, ?, ?, ?)
        ''', (post_id, chat_id, message_id, bot_name))
        conn.commit()
        conn.close()

    def get_sent_messages(self, post_id, bot_name):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT chat_id, message_id FROM sent_messages 
            WHERE post_id=? AND bot_name=?
        ''', (post_id, bot_name))
        results = cursor.fetchall()
        conn.close()
        return results

    def delete_sent_messages(self, post_id, bot_name):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM sent_messages WHERE post_id=? AND bot_name=?
        ''', (post_id, bot_name))
        conn.commit()
        conn.close()

    async def send_text_message(self, chat_id, text):
        try:
            escaped_text = escape_markdown_v2(text, preserve_markdown=True)
            message = await self.bot.send_message(
                chat_id=chat_id,
                text=escaped_text,
                parse_mode='MarkdownV2'
            )
            # Сохранение message_id для возможности удаления
            self.add_sent_message(str(uuid.uuid4()), chat_id, message.message_id, self.bot_name)
            return True
        except Exception as e:
            logging.error(f"Ошибка при отправке текста через {self.bot_name} пользователю {chat_id}: {e}")
            return False

    async def send_media_group(self, chat_id, media_list, caption=None):
        try:
            telegram_media = []
            for idx, item in enumerate(media_list):
                media_file = open(item['file_path'], 'rb')
                if item['type'] == 'photo':
                    media = InputMediaPhoto(
                        media=media_file,
                        has_spoiler=item['has_spoiler'],
                        caption=caption if idx == 0 and caption else None,
                        parse_mode='MarkdownV2' if idx == 0 and caption else None
                    )
                elif item['type'] == 'video':
                    media = InputMediaVideo(
                        media=media_file,
                        has_spoiler=item['has_spoiler'],
                        caption=caption if idx == 0 and caption else None,
                        parse_mode='MarkdownV2' if idx == 0 and caption else None
                    )
                telegram_media.append(media)
            messages = await self.bot.send_media_group(chat_id=chat_id, media=telegram_media)
            if messages:
                for message in messages:
                    self.add_sent_message(str(uuid.uuid4()), chat_id, message.message_id, self.bot_name)
                return True
            return False
        except Exception as e:
            logging.error(f"Ошибка при отправке медиагруппы через {self.bot_name} пользователю {chat_id}: {e}")
            return False

    async def send_video_note(self, chat_id, video_note_path):
        try:
            with open(video_note_path, 'rb') as vf:
                message = await self.bot.send_video_note(chat_id=chat_id, video_note=vf)
            self.add_sent_message(str(uuid.uuid4()), chat_id, message.message_id, self.bot_name)
            return True
        except Exception as e:
            logging.error(f"Ошибка при отправке видео-сообщения через {self.bot_name} пользователю {chat_id}: {e}")
            return False

    async def send_voice(self, chat_id, voice_path):
        try:
            with open(voice_path, 'rb') as af:
                message = await self.bot.send_voice(chat_id=chat_id, voice=af)
            self.add_sent_message(str(uuid.uuid4()), chat_id, message.message_id, self.bot_name)
            return True
        except Exception as e:
            logging.error(f"Ошибка при отправке аудиосообщения через {self.bot_name} пользователю {chat_id}: {e}")
            return False

    async def delete_messages(self, chat_id, message_ids):
        try:
            for message_id in message_ids:
                await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logging.error(f"Ошибка при удалении сообщения через {self.bot_name} пользователю {chat_id}: {e}")

def admin_main_menu():
    keyboard = [
        [
            KeyboardButton("📤 Отправить пост"),
            KeyboardButton("🎥 Отправить видео-сообщение")
        ],
        [
            KeyboardButton("🎤 Аудиосообщение"),
            KeyboardButton("✏️ Редактировать пост"),
            KeyboardButton("🗑 Удалить пост")
        ],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def choose_post_type_menu():
    keyboard = [
        [
            KeyboardButton("📄 Текст"),
            KeyboardButton("📷 Медиа")
        ],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def spoiler_decision_menu():
    keyboard = [
        [
            KeyboardButton("Скрыть весь текст"),
            KeyboardButton("Скрыть часть текста")
        ],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def yes_no_menu():
    keyboard = [
        [
            KeyboardButton("Да"),
            KeyboardButton("Нет")
        ],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def select_bot_menu(sending_bots):
    keyboard = []
    for bot in sending_bots:
        keyboard.append([KeyboardButton(bot.name)])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def escape_markdown_v2(text, preserve_markdown=False):
    link_pattern = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

    def escape_link(match):
        text_part = match.group(1)
        url_part = match.group(2)
        
        text_part_escaped = escape_markdown_v2_basic(text_part, preserve_markdown)

        url_part_escaped = url_part.replace('(', r'\(').replace(')', r'\)')
        return f'[{text_part_escaped}]({url_part_escaped})'

    text = link_pattern.sub(escape_link, text)

    spoiler_pattern = re.compile(r'\|\|(.+?)\|\|')

    def escape_spoiler(match):
        content = match.group(1)
        content_escaped = escape_markdown_v2_basic(content, preserve_markdown)
        return f'||{content_escaped}||'

    text = spoiler_pattern.sub(escape_spoiler, text)

    text = escape_markdown_v2_basic(text, preserve_markdown)

    return text

def escape_markdown_v2_basic(text, preserve_markdown=False):
    if preserve_markdown:
        escape_chars = r'([\\{}\#\+\-\.\!\(\):])'
    else:
        escape_chars = r'([\\`{}\#\+\-\.\!\(\)\[\]:])'
    return re.sub(escape_chars, r'\\\1', text)

def allowed_users_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ALLOWED_USER_IDS:
            logger.info(f"Неавторизованный доступ от пользователя {user_id}.")
            await update.message.reply_text("У вас нет доступа к этому боту.")
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper

async def get_user_ids(redis_client, set_name):
    user_ids = redis_client.smembers(set_name)
    try:
        user_ids = [int(uid) for uid in user_ids]
    except ValueError:
        logger.error("Некоторые chat_id не являются числами.")
        user_ids = []
    return user_ids

@allowed_users_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Здравствуйте! Выберите действие:",
        reply_markup=admin_main_menu()
    )
    return ADMIN_PANEL

@allowed_users_only
async def admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE, sending_bots):
    text = update.message.text
    if text == "📤 Отправить пост":
        context.user_data.clear()
        await update.message.reply_text(
            "Что вы хотите отправить?",
            reply_markup=choose_post_type_menu()
        )
        return SEND_POST_CHOICES
    elif text == "🎥 Отправить видео-сообщение":
        context.user_data.clear()
        await update.message.reply_text("Отправьте видео (обычное видео или как файл):", reply_markup=ReplyKeyboardRemove())
        return SEND_VIDEO_NOTE
    elif text == "🎤 Аудиосообщение":
        context.user_data.clear()
        await update.message.reply_text("Отправьте аудиосообщение (ваш голос):", reply_markup=ReplyKeyboardRemove())
        return SEND_POST_AUDIO
    elif text == "✏️ Редактировать пост":
        await update.message.reply_text("Введите ID поста для редактирования:", reply_markup=ReplyKeyboardRemove())
        context.user_data['action'] = 'edit'
        return SELECT_POST
    elif text == "🗑 Удалить пост":
        await update.message.reply_text("Введите ID поста для удаления:", reply_markup=ReplyKeyboardRemove())
        context.user_data['action'] = 'delete'
        return SELECT_POST
    else:
        await update.message.reply_text("Пожалуйста, выберите действие с помощью кнопок.", reply_markup=admin_main_menu())
        return ADMIN_PANEL

@allowed_users_only
async def choose_post_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📄 Текст":
        await update.message.reply_text(
            "Введите текст поста (поддерживаются встроенные форматы Telegram):",
            reply_markup=ReplyKeyboardRemove()
        )
        return SEND_POST_TEXT
    elif text == "📷 Медиа":
        await update.message.reply_text(
            "Загрузите медиафайлы (фото или видео). После загрузки каждого файла нажмите /done для завершения.",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data['media'] = []
        return SEND_POST_MEDIA
    elif text == "📄 + 📷 + 🎤 Текст, Медиа и Аудио":
        await update.message.reply_text(
            "Введите текст поста (поддерживаются встроенные форматы Telegram):",
            reply_markup=ReplyKeyboardRemove()
        )
        return SEND_BOTH_TEXT
    else:
        await update.message.reply_text(
            "Пожалуйста, выберите один из предложенных вариантов.",
            reply_markup=choose_post_type_menu()
        )
        return SEND_POST_CHOICES

@allowed_users_only
async def send_post_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data['post_text'] = text
    await update.message.reply_text(
        "Хотите ли вы скрыть текст под спойлером?",
        reply_markup=yes_no_menu()
    )
    return SPOILER_DECISION_TEXT

@allowed_users_only
async def spoiler_decision_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    decision = update.message.text.lower()
    if decision in ['да', 'д']:
        await update.message.reply_text(
            "Выберите, что скрыть:",
            reply_markup=spoiler_decision_menu()
        )
        return SPOILER_PART_DECISION_TEXT
    elif decision in ['нет', 'н']:
        context.user_data['spoiler_text'] = False
        await update.message.reply_text(
            "Текст будет отправлен без спойлера.\n\nТеперь отправьте медиа-файлы (по одному) или введите /done для завершения.",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data['media'] = []
        return SEND_POST_MEDIA
    else:
        await update.message.reply_text(
            "Пожалуйста, ответьте 'Да' или 'Нет'.\nХотите ли вы скрыть текст под спойлером?",
            reply_markup=yes_no_menu()
        )
        return SPOILER_DECISION_TEXT

@allowed_users_only
async def spoiler_part_decision_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    decision = update.message.text.lower()
    if decision == "скрыть весь текст":
        context.user_data['spoiler_text'] = 'full'

        original_text = context.user_data.get('post_text', '')
        context.user_data['post_text'] = f"||{original_text}||"
        await update.message.reply_text(
            "Текст полностью скрыт под спойлером.\n\nТеперь отправьте медиа-файлы (по одному) или введите /done для завершения.",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data['media'] = []
        return SEND_POST_MEDIA
    elif decision == "скрыть часть текста":
        context.user_data['spoiler_text'] = 'partial'
        await update.message.reply_text(
            "Введите фрагмент текста, который нужно скрыть под спойлером:",
            reply_markup=ReplyKeyboardRemove()
        )
        return HIDE_TEXT_FRAGMENT
    else:
        await update.message.reply_text(
            "Пожалуйста, выберите один из предложенных вариантов.",
            reply_markup=spoiler_decision_menu()
        )
        return SPOILER_PART_DECISION_TEXT

@allowed_users_only
async def hide_text_fragment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fragment = update.message.text
    original_text = context.user_data.get('post_text', '')

    cleaned_original_text = original_text.replace('||', '')
    if fragment not in cleaned_original_text:
        await update.message.reply_text(
            "Фрагмент не найден в тексте. Пожалуйста, введите корректный фрагмент."
        )
        return HIDE_TEXT_FRAGMENT

    spoiler_fragment = f"||{fragment}||"
    formatted_text = original_text.replace(fragment, spoiler_fragment, 1) 
    context.user_data['post_text'] = formatted_text
    await update.message.reply_text(
        "Фрагмент текста скрыт под спойлером.\n\nТеперь отправьте медиа-файлы (по одному) или введите /done для завершения.",
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data['media'] = []
    return SEND_POST_MEDIA

@allowed_users_only
async def send_post_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_obj = update.message.photo[-1]
        file_type = 'photo'
        suffix = '.jpg'
    elif update.message.video:
        file_obj = update.message.video
        file_type = 'video'
        suffix = '.mp4'
    elif update.message.document and update.message.document.mime_type.startswith('video/'):
        file_obj = update.message.document
        file_type = 'video'
        suffix = '.mp4'
    else:
        await update.message.reply_text("Пожалуйста, отправьте фото или видео.")
        return SEND_POST_MEDIA

    try:
        file = await file_obj.get_file()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            await file.download_to_drive(temp_file.name)
            temp_file_path = temp_file.name
    except Exception as e:
        logging.error(f"Ошибка при скачивании файла: {e}")
        await update.message.reply_text("Не удалось загрузить файл. Попробуйте снова.")
        return SEND_POST_MEDIA

    context.user_data['current_media'] = temp_file_path
    context.user_data['current_media_type'] = file_type
    await update.message.reply_text("Скрыть это медиа под спойлером? (Да/Нет)", reply_markup=yes_no_menu())
    return SPOILER_DECISION_MEDIA

@allowed_users_only
async def spoiler_decision_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    decision = update.message.text.lower()
    if decision in ['да', 'д']:
        has_spoiler = True
    elif decision in ['нет', 'н']:
        has_spoiler = False
    else:
        await update.message.reply_text("Пожалуйста, ответьте 'Да' или 'Нет'.\nСкрыть это медиа под спойлером?",
                                        reply_markup=yes_no_menu())
        return SPOILER_DECISION_MEDIA

    media_list = context.user_data.get('media', [])
    media_type = context.user_data.get('current_media_type')
    file_path = context.user_data.get('current_media')

    media_list.append({
        'type': media_type,
        'file_path': file_path,
        'has_spoiler': has_spoiler
    })

    context.user_data['media'] = media_list
    context.user_data['current_media'] = None
    context.user_data['current_media_type'] = None

    await update.message.reply_text("Медиафайл добавлен. Отправьте следующий или введите /done для завершения.")
    return SEND_POST_MEDIA

@allowed_users_only
async def send_both_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data['post_text'] = text
    await update.message.reply_text(
        "Хотите ли вы скрыть текст под спойлером?",
        reply_markup=yes_no_menu()
    )
    return SPOILER_DECISION_TEXT

@allowed_users_only
async def done_send_post_media(update: Update, context: ContextTypes.DEFAULT_TYPE, sending_bots):
    media = context.user_data.get('media', [])
    post_text = context.user_data.get('post_text', '')
    post_id = str(uuid.uuid4())
    
    # Определяем тип поста
    if post_text and media:
        post_type = 'text_media'
        data = json.dumps(media)
        content = post_text
    elif post_text:
        post_type = 'text'
        data = None
        content = post_text
    elif media:
        post_type = 'media'
        data = json.dumps(media)
        content = ''
    else:
        await update.message.reply_text("Нечего отправлять. Пожалуйста, начните заново.", reply_markup=admin_main_menu())
        return ADMIN_PANEL
    

    context.user_data['post_id'] = post_id
    context.user_data['post_content'] = content
    context.user_data['post_type'] = post_type
    context.user_data['post_data'] = data
    
    # Запрашиваем выбор бота для публикации
    await update.message.reply_text(
        "Выберите бота, в которого опубликовать пост:",
        reply_markup=select_bot_menu(sending_bots)
    )
    return SELECT_BOT_POST

@allowed_users_only
async def receive_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE, sending_bots):
    video = None
    if update.message.video:
        video = update.message.video
    elif update.message.document and update.message.document.mime_type.startswith('video/'):
        video = update.message.document

    if video:
        try:
            file = await video.get_file()
            suffix = '.mp4'
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
                await file.download_to_drive(temp_file.name)
                original_video_path = temp_file.name
        except Exception as e:
            logging.error(f"Ошибка при скачивании видео: {e}")
            await update.message.reply_text("Не удалось загрузить видео. Попробуйте снова.")
            return SEND_VIDEO_NOTE

        try:
            video_clip = VideoFileClip(original_video_path)
            video_size = os.path.getsize(original_video_path)
            video_duration = video_clip.duration
            width, height = video_clip.size

            # Требования Telegram для video_note:
            # Максимальный размер: 50 МБ
            # Максимальная длина: 60 секунд
            # Соотношение сторон: 1:1 (квадратное видео)

            if video_size > 50 * 1024 * 1024 or video_duration > 60:
                # Сжатие видео
                logging.info(f"Видео слишком большое или длинное. Начинаем сжатие. Размер: {video_size} байт, Длительность: {video_duration} сек.")
                compressed_video_path = original_video_path.replace('.mp4', '_compressed.mp4')
                try:
                    video_clip.resize(height=min(width, height)).set_duration(min(video_duration, 60)).write_videofile(compressed_video_path, codec='libx264', audio_codec='aac', fps=24) # fps = frames per second
                    os.remove(original_video_path)
                    video_clip = VideoFileClip(compressed_video_path)
                    video_size = os.path.getsize(compressed_video_path)
                    video_duration = video_clip.duration
                    width, height = video_clip.size
                    logging.info(f"Видео успешно сжато. Новый размер: {video_size} байт, Новая длительность: {video_duration} сек.")

                except Exception as e:
                    logging.error(f"Ошибка при сжатии видео: {e}")
                    await update.message.reply_text("Не удалось сжать видео.")
                    os.remove(original_video_path)
                    return SEND_VIDEO_NOTE


            # Проверка соотношения сторон (1:1)
            if abs(width - height) > 10:  # Допустимая погрешность
                await update.message.reply_text("Видео не соответствует формату 1:1 (квадратное видео).")
                await update.message.reply_text("Бот автоматически приведёт видео к формату 1:1.")

                new_size = min(width, height)
                cropped_video = video_clip.crop(x_center=width/2, y_center=height/2, width=new_size, height=new_size)
                cropped_video_path = original_video_path.replace('.mp4', '_cropped.mp4')
                cropped_video.write_videofile(cropped_video_path, codec='libx264', audio_codec='aac', fps=24)
                if os.path.exists(original_video_path):
                    os.remove(original_video_path)
                if os.path.exists(compressed_video_path):
                    os.remove(compressed_video_path)
                video_clip = VideoFileClip(cropped_video_path)
                video_size = os.path.getsize(cropped_video_path)
                temp_file_path = cropped_video_path
            else:
                temp_file_path = compressed_video_path if os.path.exists(compressed_video_path) else original_video_path
                if os.path.exists(original_video_path) and original_video_path != temp_file_path:
                    os.remove(original_video_path)
                elif os.path.exists(compressed_video_path) and compressed_video_path != temp_file_path:
                    os.remove(compressed_video_path)

            if video_size > 50 * 1024 * 1024:
                await update.message.reply_text("Видео слишком большое для видео-сообщения (максимум 50МБ) даже после сжатия.")
                os.remove(temp_file_path)
                return SEND_VIDEO_NOTE

        except Exception as e:
            logging.error(f"Ошибка при обработке видео: {e}")
            await update.message.reply_text("Не удалось обработать видео.")
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            return SEND_VIDEO_NOTE

        context.user_data['video_path'] = temp_file_path

        await update.message.reply_text(
            "Выберите бота, через которого отправить видео-сообщение:",
            reply_markup=select_bot_menu(sending_bots)
        )
        return SELECT_BOT_VIDEO_AUDIO
    else:
        await update.message.reply_text("Пожалуйста, отправьте видео или видеофайл.")
        return SEND_VIDEO_NOTE


@allowed_users_only
async def receive_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, sending_bots):
    if update.message.voice:
        voice = update.message.voice

        try:
            file = await voice.get_file()
            with tempfile.NamedTemporaryFile(delete=False, suffix='.ogg') as temp_file:
                await file.download_to_drive(temp_file.name)
                temp_file_path = temp_file.name
        except Exception as e:
            logging.error(f"Ошибка при скачивании аудиосообщения: {e}")
            await update.message.reply_text("Не удалось загрузить аудиосообщение. Попробуйте снова.")
            return SEND_POST_AUDIO

        context.user_data['voice_path'] = temp_file_path
        await update.message.reply_text(
            "Выберите бота, через которого отправить аудиосообщение:",
            reply_markup=select_bot_menu(sending_bots)
        )
        return SELECT_BOT_VIDEO_AUDIO
    else:
        await update.message.reply_text("Пожалуйста, отправьте аудиосообщение (ваш голос).")
        return SEND_POST_AUDIO

@allowed_users_only
async def select_post_action(update: Update, context: ContextTypes.DEFAULT_TYPE, sending_bots):
    post_id = update.message.text.strip()
    action = context.user_data.get('action')


    posts_found = []
    for bot_manager in sending_bots:
        post_data = bot_manager.get_post(post_id, bot_manager.bot_name)
        if post_data:
            posts_found.append(bot_manager)

    if not posts_found:
        await update.message.reply_text("Пост с таким ID не найден. Попробуйте ещё раз.")
        return SELECT_POST

    context.user_data['post_id'] = post_id

    if action == 'edit':
        await update.message.reply_text("Введите новый текст для поста (поддерживаются встроенные форматы Telegram):")
        return EDIT_POST
    elif action == 'delete':

        total_deleted = 0
        for bot_manager in posts_found:
            post_data = bot_manager.get_post(post_id, bot_manager.bot_name)
            if not post_data:
                continue
            _, post_type, data = post_data
            sent_msgs = bot_manager.get_sent_messages(post_id, bot_manager.bot_name)
            for chat_id, message_id in sent_msgs:
                try:
                    await bot_manager.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    total_deleted += 1
                except Exception as e:
                    logging.error(f"Ошибка при удалении сообщения у пользователя {chat_id} через {bot_manager.bot_name}: {e}")
            bot_manager.delete_sent_messages(post_id, bot_manager.bot_name)
            bot_manager.delete_post(post_id, bot_manager.bot_name)
        final_message = f"Пост удалён."
        escaped_final_message = escape_markdown_v2(final_message)
        await update.message.reply_text(escaped_final_message, parse_mode='MarkdownV2')
        await update.message.reply_text("Выберите следующее действие:", reply_markup=admin_main_menu())
        return ADMIN_PANEL
    else:
        await update.message.reply_text("Неизвестное действие.")
        return ADMIN_PANEL

@allowed_users_only
async def edit_post_text(update: Update, context: ContextTypes.DEFAULT_TYPE, sending_bots):
    new_text = update.message.text
    post_id = context.user_data.get('post_id')


    for bot_manager in sending_bots:
        post_data = bot_manager.get_post(post_id, bot_manager.bot_name)
        if not post_data:
            continue
        _, post_type, data = post_data
        bot_manager.save_post(post_id, new_text, post_type, data, bot_manager.bot_name)
        sent_msgs = bot_manager.get_sent_messages(post_id, bot_manager.bot_name)
        escaped_text = escape_markdown_v2(new_text, preserve_markdown=True)
        for chat_id, message_id in sent_msgs:
            try:
                if post_type == 'text':
                    await bot_manager.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=escaped_text, parse_mode='MarkdownV2')
                elif post_type == 'text_media':
                    await bot_manager.bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=escaped_text, parse_mode='MarkdownV2')

            except Exception as e:
                logging.error(f"Ошибка при редактировании сообщения у пользователя {chat_id} через {bot_manager.bot_name}: {e}")

    final_message = f"Пост обновлён."
    escaped_final_message = escape_markdown_v2(final_message)
    await update.message.reply_text(escaped_final_message, parse_mode='MarkdownV2')
    await update.message.reply_text("Выберите следующее действие:", reply_markup=admin_main_menu())
    return ADMIN_PANEL

@allowed_users_only
async def select_bot_video_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, sending_bots):
    selected_bot_name = update.message.text.strip()
    selected_bots = [bot for bot in sending_bots if bot.name.lower() == selected_bot_name.lower()]
    if not selected_bots:
        await update.message.reply_text(
            "Выбранный бот не найден. Пожалуйста, выберите бота из списка.",
            reply_markup=select_bot_menu(sending_bots)
        )
        return SELECT_BOT_VIDEO_AUDIO
    selected_bot = selected_bots[0]


    if 'video_path' in context.user_data:
        video_path = context.user_data['video_path']
        del context.user_data['video_path']
        try:
            user_ids = await get_user_ids(selected_bot.redis_client, selected_bot.chat_id_set)
            successful = 0
            post_id = str(uuid.uuid4())
            # Сохраняем пост
            selected_bot.save_post(post_id, '', 'video_note', video_path, selected_bot.bot_name)
            for chat_id in user_ids:
                try:

                    success = await selected_bot.send_video_note(chat_id, video_path)
                    if success:
                        successful += 1
                except Exception as e:
                    logging.error(f"Ошибка при отправке видео-сообщения пользователю {chat_id} через {selected_bot.bot_name}: {e}")
            try:
                os.remove(video_path)
            except Exception as e:
                logging.error(f"Не удалось удалить временный файл {video_path}: {e}")
            final_message = f"Видеосообщение отправлено через бота {selected_bot.name}.\nID поста: {post_id}."
            escaped_final_message = escape_markdown_v2(final_message)
            await update.message.reply_text(escaped_final_message, parse_mode='MarkdownV2')
            await update.message.reply_text("Выберите следующее действие:", reply_markup=admin_main_menu())
            return ADMIN_PANEL
        except Exception as e:
            logging.error(f"Ошибка при отправке видео-сообщения: {e}")
            await update.message.reply_text("Произошла ошибка при отправке видео-сообщения.")
            return ADMIN_PANEL

    elif 'voice_path' in context.user_data:
        voice_path = context.user_data['voice_path']
        del context.user_data['voice_path']
        try:
            user_ids = await get_user_ids(selected_bot.redis_client, selected_bot.chat_id_set)
            successful = 0
            post_id = str(uuid.uuid4())

            selected_bot.save_post(post_id, '', 'audio', voice_path, selected_bot.bot_name)
            for chat_id in user_ids:
                try:
                    success = await selected_bot.send_voice(chat_id, voice_path)
                    if success:
                        successful += 1
                except Exception as e:
                    logging.error(f"Ошибка при отправке аудиосообщения пользователю {chat_id} через {selected_bot.bot_name}: {e}")
            try:
                os.remove(voice_path)
            except Exception as e:
                logging.error(f"Не удалось удалить временный файл {voice_path}: {e}")
            final_message = f"Аудиосообщение отправлено через бота {selected_bot.name}.\nID поста: {post_id}."
            escaped_final_message = escape_markdown_v2(final_message)
            await update.message.reply_text(escaped_final_message, parse_mode='MarkdownV2')
            await update.message.reply_text("Выберите следующее действие:", reply_markup=admin_main_menu())
            return ADMIN_PANEL
        except Exception as e:
            logging.error(f"Ошибка при отправке аудиосообщения: {e}")
            await update.message.reply_text("Произошла ошибка при отправке аудиосообщения.")
            return ADMIN_PANEL
    else:
        await update.message.reply_text("Неизвестная ошибка. Пожалуйста, попробуйте снова.")
        return ADMIN_PANEL

@allowed_users_only
async def select_bot_post(update: Update, context: ContextTypes.DEFAULT_TYPE, sending_bots):
    selected_bot_name = update.message.text.strip()
    selected_bots = [bot for bot in sending_bots if bot.name.lower() == selected_bot_name.lower()]
    if not selected_bots:
        await update.message.reply_text(
            "Выбранный бот не найден. Пожалуйста, выберите бота из списка.",
            reply_markup=select_bot_menu(sending_bots)
        )
        return SELECT_BOT_POST
    selected_bot = selected_bots[0]
    

    post_id = context.user_data.get('post_id')
    content = context.user_data.get('post_content')
    post_type = context.user_data.get('post_type')
    data = context.user_data.get('post_data')
    

    selected_bot.save_post(post_id, content, post_type, data, selected_bot.bot_name)
    

    all_successful = 0
    user_ids = await get_user_ids(selected_bot.redis_client, selected_bot.chat_id_set)
    successful = 0
    for chat_id in user_ids:
        try:
            if post_type == 'text':
                success = await selected_bot.send_text_message(chat_id, content)
                if success:
                    successful += 1
            elif post_type == 'media':
                media = json.loads(data)
                success = await selected_bot.send_media_group(chat_id, media)
                if success:
                    successful += 1
            elif post_type == 'text_media':
                media = json.loads(data)
                success = await selected_bot.send_media_group(chat_id, media, caption=content)
                if success:
                    successful += 1
        except Exception as e:
            logging.error(f"Ошибка при отправке поста пользователю {chat_id} через {selected_bot.name}: {e}")
    all_successful += successful
    

    final_message = f"Пост отправлен через бота {selected_bot.name}.\nID поста: {post_id}."
    escaped_final_message = escape_markdown_v2(final_message)
    await update.message.reply_text(escaped_final_message, parse_mode='MarkdownV2')
    

    context.user_data.clear()
    

    await update.message.reply_text("Выберите следующее действие:", reply_markup=admin_main_menu())
    return ADMIN_PANEL

@allowed_users_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, sending_bots):
    media = context.user_data.get('media', [])
    for item in media:
        file_path = item.get('file_path')
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logging.error(f"Не удалось удалить временный файл {file_path}: {e}")
    voice_path = context.user_data.get('voice_path')
    if voice_path and os.path.exists(voice_path):
        try:
            os.remove(voice_path)
        except Exception as e:
            logging.error(f"Не удалось удалить временный файл {voice_path}: {e}")

    video_path = context.user_data.get('video_path')
    if video_path and os.path.exists(video_path):
        try:
            os.remove(video_path)
        except Exception as e:
            logging.error(f"Не удалось удалить временный файл {video_path}: {e}")

    keys_to_remove = ['spoiler_text', 'post_text', 'media', 'current_media', 'current_media_type', 
                      'audio', 'current_audio', 'action', 'post_id', 'voice_path', 'video_path']
    for key in keys_to_remove:
        if key in context.user_data:
            del context.user_data[key]
    await update.message.reply_text(
        "Действие отменено. Возвращаюсь в главное меню.",
        reply_markup=admin_main_menu()
    )
    return ADMIN_PANEL

@allowed_users_only
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Не понимаю ваш запрос. Пожалуйста, используйте кнопки меню.",
        reply_markup=admin_main_menu()
    )
    return ADMIN_PANEL

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит все исключения, которые не были обработаны ранее."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

def main():

    sending_bots_configs = [
        {
            'BOT_TOKEN': os.getenv('CAPTAIN_BOT_TOKEN'),
            'REDIS_HOST': os.getenv('CAPTAIN_REDIS_HOST'),
            'REDIS_PORT': os.getenv('CAPTAIN_REDIS_PORT'),
            'REDIS_USERNAME': os.getenv('CAPTAIN_REDIS_USERNAME'),
            'REDIS_PASSWORD': os.getenv('CAPTAIN_REDIS_PASSWORD'),
            'REDIS_DB': os.getenv('CAPTAIN_REDIS_DB'),
            'CHAT_ID_COLUMN': os.getenv('CAPTAIN_CHAT_ID_COLUMN'),
            'DB_PATH': os.getenv('CAPTAIN_DB_PATH'),
            'name': 'Captain'
        },
        {
            'BOT_TOKEN': os.getenv('WEST_BOT_TOKEN'),
            'REDIS_HOST': os.getenv('WEST_REDIS_HOST'),
            'REDIS_PORT': os.getenv('WEST_REDIS_PORT'),
            'REDIS_USERNAME': os.getenv('WEST_REDIS_USERNAME'),
            'REDIS_PASSWORD': os.getenv('WEST_REDIS_PASSWORD'),
            'REDIS_DB': os.getenv('WEST_REDIS_DB'),
            'CHAT_ID_COLUMN': os.getenv('WEST_CHAT_ID_COLUMN'),
            'DB_PATH': os.getenv('WEST_DB_PATH'),
            'name': 'West'
        }
    ]

    sending_bots = []
    for config in sending_bots_configs:
        if not all([config.get('BOT_TOKEN'), config.get('REDIS_HOST'), config.get('REDIS_PORT'),
                    config.get('REDIS_USERNAME'), config.get('REDIS_PASSWORD'),
                    config.get('REDIS_DB'), config.get('CHAT_ID_COLUMN'), config.get('DB_PATH')]):
            logger.warning(f"Недостаточно конфигурации для бота {config.get('name', 'Unknown')}. Пропуск.")
            continue
        sending_bots.append(SendingBotManager(config['name'], config))

    if not sending_bots:
        logger.error("Нет настроенных отправляющих ботов. Завершаем работу.")
        return

    # Создаем админское приложение
    admin_app = ApplicationBuilder().token(ADMIN_BOT_TOKEN).build()
    admin_app.add_error_handler(error_handler)


    allowed_user_ids = ALLOWED_USER_IDS

    # Добавляем обработчики разговоров
    conversation_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            ADMIN_PANEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda update, context: admin_commands(update, context, sending_bots)),
            ],
            SEND_POST_CHOICES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, choose_post_type),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SEND_POST_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, send_post_text),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SPOILER_DECISION_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, spoiler_decision_text),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SPOILER_PART_DECISION_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, spoiler_part_decision_text),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            HIDE_TEXT_FRAGMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, hide_text_fragment),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SEND_POST_MEDIA: [
                MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.VIDEO, send_post_media),
                CommandHandler('done', lambda update, context: done_send_post_media(update, context, sending_bots)),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SPOILER_DECISION_MEDIA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, spoiler_decision_media),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SEND_POST_AUDIO: [
                MessageHandler(filters.VOICE, lambda update, context: receive_audio(update, context, sending_bots)),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SEND_VIDEO_NOTE: [
                MessageHandler(filters.VIDEO | filters.Document.VIDEO, lambda update, context: receive_video_note(update, context, sending_bots)),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SEND_BOTH_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, send_both_text),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SELECT_POST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda update, context: select_post_action(update, context, sending_bots)),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            EDIT_POST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda update, context: edit_post_text(update, context, sending_bots)),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SELECT_BOT_VIDEO_AUDIO: [ 
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda update, context: select_bot_video_audio(update, context, sending_bots)),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SELECT_BOT_POST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda update, context: select_bot_post(update, context, sending_bots)),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
            SELECT_BOT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, select_bot_post),
                CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            ],
        },
        fallbacks=[
            CommandHandler('cancel', lambda update, context: cancel(update, context, sending_bots)),
            MessageHandler(filters.ALL, unknown)
        ],
    )
    admin_app.add_handler(conversation_handler)

    print("Админский бот запущен...")
    admin_app.run_polling()



if __name__ == '__main__':
    main()

import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import anthropic
import json
from datetime import datetime
from enum import Enum
import base64
import io
import asyncio
import requests
import traceback

# Получаем токены из переменных окружения
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')

# ========== ИНТЕГРАЦИЯ N8N ==========
# URL для отправки ошибок в n8n
N8N_WEBHOOK = "https://noboring.app.n8n.cloud/webhook-test/telegram-errors"

def report_error(error_description):
    """Отправляет ошибку в n8n для создания GitHub Issue"""
    try:
        requests.post(N8N_WEBHOOK, json={"text": error_description}, timeout=5)
        print(f"✅ Ошибка отправлена в n8n")
    except Exception as e:
        print(f"❌ Не удалось отправить ошибку в n8n: {e}")
# ====================================

import httpx

# Создаем клиент с явными настройками (если доступен ключ)
if ANTHROPIC_API_KEY:
    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        max_retries=2,
        timeout=httpx.Timeout(60.0, connect=10.0)
    )
else:
    client = None

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")

class SessionState(Enum):
    WAITING_TASK = "waiting_task"
    SOLVING = "solving"
    FINAL_ANSWER = "final_answer"
    CHECKING = "checking"
    COMPLETED = "completed"
    EXAM_MODE = "exam_mode"

# Системный промпт для Сократовского метода
SYSTEM_PROMPT = """Ты математический наставник, который использует Сократовский метод обучения.

ПРАВИЛА:
1. НИКОГДА не давай прямые ответы на задачи
2. Веди ученика к решению через наводящие вопросы
3. Разбивай сложные задачи на простые шаги
4. Оценивай уровень понимания по ответам ученика
5. Адаптируй сложность вопросов под уровень ученика
6. Хвали за правильные шаги и мышление
7. Если ученик застрял - дай подсказку, но не решение
8. Проверяй понимание концепций, а не только вычисления

СТРАТЕГИЯ:
- Сначала убедись, что ученик понимает условие задачи
- Определи, какие концепции нужны для решения
- Проверь, знает ли ученик эти концепции
- Веди через небольшие шаги к решению
- После каждого шага проверяй понимание

СТИЛЬ:
- Дружелюбный и поддерживающий
- Задавай один вопрос за раз
- Используй emoji для эмоциональной поддержки (умеренно)
- Говори на русском языке

Если ученик просит прямой ответ, объясни ценность самостоятельного решения."""

VERIFICATION_PROMPT = """Ты проверяющий математических решений.

ЗАДАЧА: {original_task}

ОТВЕТ УЧЕНИКА: {student_answer}

Твоя задача:
1. Проверь правильность финального ответа
2. Оцени качество решения (логика, шаги, обоснование)
3. Укажи ошибки если есть
4. Дай конструктивную обратную связь

Формат ответа (JSON):
{{
  "correct": true/false,
  "final_answer": "правильный ответ",
  "score": 0-100,
  "feedback": "детальная обратная связь",
  "mistakes": ["список ошибок если есть"],
  "strengths": ["что ученик сделал хорошо"]
}}"""

MEME_GENERATION_PROMPT = """Создай веселый мем-текст для ученика, который только что решил математическую задачу.

КОНТЕКСТ:
- Оценка: {score}/100
- Задача была: {task_type}
- Уровень: {difficulty}

ТРЕБОВАНИЯ:
1. Мем должен быть актуальным и современным (2024-2025)
2. Используй популярные форматы мемов (но без упоминания конкретных картинок)
3. Связан с математикой и учебой
4. Позитивный и мотивирующий
5. Понятен подросткам и студентам
6. Не длиннее 2-3 строк
7. Можно использовать сленг и интернет-культуру

СТИЛЬ зависит от оценки:
- 80-100: Эпичная победа, "based", "gigachad energy"
- 60-79: Хорошая работа, "respectable", "W"
- 40-59: Поддержка, "we take those", "small wins"
- 0-39: Мотивация, "character development", "learning arc"

Формат ответа - только текст мема, без пояснений."""

# Математическая клавиатура
MATH_SYMBOLS = {
    'basic': ['√', '²', '³', '∫', 'π', '±', '÷', '×'],
    'greek': ['α', 'β', 'γ', 'δ', 'θ', 'λ', 'μ', 'σ'],
    'calculus': ['∑', '∏', '∂', '∇', '∞', '≈', '≠', '≤', '≥'],
    'geometry': ['∠', '°', '⊥', '∥', '△', '□', '○']
}

class UserSession:
    def __init__(self, user_id):
        self.user_id = user_id
        self.state = SessionState.WAITING_TASK
        self.current_task = None
        self.conversation = []
        self.stats = {
            "total_tasks": 0,
            "completed_tasks": 0,
            "average_score": 0,
            "total_hints": 0,
            "tasks_history": [],
            "total_memes_earned": 0
        }
        self.difficulty = "medium"
        self.exam_mode = False
        self.task_start_time = None
        self.temp_task = None
        self.meme_enabled = True

user_sessions = {}

def get_session(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession(user_id)
    return user_sessions[user_id]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        session = get_session(user_id)
        
        keyboard = [
            [InlineKeyboardButton("📚 Начать решать", callback_data="start_solving")],
            [InlineKeyboardButton("📊 Моя статистика", callback_data="show_stats")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
            [InlineKeyboardButton("❓ Помощь", callback_data="help")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "👋 Привет! Я твой математический наставник.\n\n"
            "🎯 Я помогу тебе научиться решать задачи самостоятельно "
            "через наводящие вопросы.\n\n"
            "✨ Возможности:\n"
            "• Пошаговое решение с проверкой понимания\n"
            "• Отслеживание твоего прогресса\n"
            "• Адаптация под твой уровень\n"
            "• Режим экзамена для самопроверки\n"
            "• 📸 Распознавание задач с фото!\n"
            "• 🎭 Веселые мемы за решенные задачи!\n\n"
            "Выбери действие:",
            reply_markup=reply_markup
        )
    except Exception as e:
        error_text = f"""Ошибка в команде /start:

**Текст ошибки:** {str(e)}

**User ID:** {update.effective_user.id}
**Username:** @{update.effective_user.username if update.effective_user.username else 'No username'}

**Трейсбек:**
```
{traceback.format_exc()}
```"""
        report_error(error_text)
        await update.message.reply_text("😔 Произошла ошибка при запуске. Попробуйте еще раз.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    # Обработка математических символов
    data = query.data
    
    if data.startswith('cat_'):
        category = data.replace('cat_', '')
        symbols = MATH_SYMBOLS.get(category, [])
        
        keyboard = []
        row = []
        for i, symbol in enumerate(symbols):
            row.append(InlineKeyboardButton(symbol, callback_data=f'sym_{symbol}'))
            if len(row) == 4 or i == len(symbols) - 1:
                keyboard.append(row)
                row = []
        
        keyboard.append([InlineKeyboardButton("« Назад", callback_data='back_menu')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f'Символы ({category}):', reply_markup=reply_markup)
        return
    
    elif data.startswith('sym_'):
        symbol = data.replace('sym_', '')
        await query.edit_message_text(f'Скопируй символ: {symbol}')
        return
    
    elif data == 'back_menu':
        keyboard = [
            [InlineKeyboardButton("Базовые", callback_data='cat_basic'),
             InlineKeyboardButton("Греческие", callback_data='cat_greek')],
            [InlineKeyboardButton("Матанализ", callback_data='cat_calculus'),
             InlineKeyboardButton("Геометрия", callback_data='cat_geometry')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text('Выбери категорию:', reply_markup=reply_markup)
        return
    
    # Остальная обработка кнопок
    if query.data == "start_solving":
        session.state = SessionState.WAITING_TASK
        await query.edit_message_text(
            "📝 Отлично! Отправь мне математическую задачу:\n\n"
            "✍️ Напиши текстом\n"
            "📸 Или пришли фото с задачей\n\n"
            "Примеры:\n"
            "• Реши уравнение: 3x + 7 = 22\n"
            "• Найди производную: f(x) = x² + 3x - 5\n"
            "• Упрости: (2x + 3)(x - 4)"
        )
    
    elif query.data == "show_stats":
        await show_statistics(query, session)
    
    elif query.data == "settings":
        await show_settings(query, session)
    
    elif query.data == "help":
        await show_help(query)
    
    elif query.data.startswith("difficulty_"):
        difficulty = query.data.replace("difficulty_", "")
        session.difficulty = difficulty
        await query.edit_message_text(
            f"✅ Уровень сложности изменен на: {difficulty}\n\n"
            "Отправь задачу для начала!"
        )
    
    elif query.data == "toggle_exam":
        session.exam_mode = not session.exam_mode
        mode = "включен" if session.exam_mode else "выключен"
        await query.edit_message_text(
            f"🎓 Режим экзамена {mode}\n\n"
            f"{'В этом режиме подсказки ограничены' if session.exam_mode else 'Обычный режим с полными подсказками'}"
        )
    
    elif query.data == "toggle_memes":
        session.meme_enabled = not session.meme_enabled
        status = "включены" if session.meme_enabled else "выключены"
        await query.edit_message_text(
            f"🎭 Мемы {status}\n\n"
            f"{'Будешь получать веселые мемы за решенные задачи!' if session.meme_enabled else 'Мемы отключены. Серьезный режим.'}"
        )
    
    elif query.data == "submit_answer":
        session.state = SessionState.FINAL_ANSWER
        await query.edit_message_text(
            "✍️ Отлично! Теперь напиши свой ФИНАЛЬНЫЙ ОТВЕТ на задачу.\n\n"
            "Постарайся написать полное решение с обоснованием."
        )
    
    elif query.data == "confirm_task":
        if hasattr(session, 'temp_task'):
            task_text = session.temp_task
            delattr(session, 'temp_task')
            
            session.current_task = task_text
            session.conversation = []
            session.state = SessionState.SOLVING
            session.task_start_time = datetime.now()
            session.stats["total_tasks"] += 1
            
            session.conversation.append({
                "role": "user",
                "content": f"Ученик хочет решить задачу: {task_text}\n\nНачни с проверки понимания условия задачи."
            })
            
            response = await get_ai_response(session, SYSTEM_PROMPT)
            
            keyboard = [
                [InlineKeyboardButton("✅ Сдать ответ", callback_data="submit_answer")],
                [InlineKeyboardButton("💡 Подсказка", callback_data="hint")],
                [InlineKeyboardButton("🔄 Начать заново", callback_data="start_solving")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"📝 Отлично! Начинаем решать!\n\n{response}",
                reply_markup=reply_markup
            )
    
    elif query.data == "edit_task":
        await query.edit_message_text(
            "✏️ Хорошо! Напиши правильный текст задачи вручную."
        )
        session.state = SessionState.WAITING_TASK
    
    elif query.data == "retry_photo":
        await query.edit_message_text(
            "📸 Хорошо! Отправь новое фото задачи."
        )
        session.state = SessionState.WAITING_TASK
    
    elif query.data == "hint":
        if session.state != SessionState.SOLVING:
            await query.answer("Сначала начни решать задачу!", show_alert=True)
            return
        
        session.stats["total_hints"] += 1
        
        session.conversation.append({
            "role": "user",
            "content": "Мне нужна подсказка. Дай небольшую подсказку, но не решение."
        })
        
        response = await get_ai_response(session, SYSTEM_PROMPT)
        
        await query.message.reply_text(f"💡 {response}")

async def show_statistics(query, session):
    text = build_statistics_text(session)
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="start_solving")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_settings(query, session):
    difficulty_emoji = {
        "easy": "🟢 Легкий",
        "medium": "🟡 Средний",
        "hard": "🔴 Сложный"
    }
    
    keyboard = [
        [InlineKeyboardButton(f"Уровень: {difficulty_emoji[session.difficulty]}", callback_data="diff_menu")],
        [InlineKeyboardButton(
            f"🎓 Режим экзамена: {'✅ Вкл' if session.exam_mode else '❌ Выкл'}", 
            callback_data="toggle_exam"
        )],
        [InlineKeyboardButton(
            f"🎭 Мемы: {'✅ Вкл' if session.meme_enabled else '❌ Выкл'}", 
            callback_data="toggle_memes"
        )],
        [InlineKeyboardButton("🔙 Назад", callback_data="start_solving")]
    ]
    
    await query.edit_message_text(
        "⚙️ НАСТРОЙКИ\n\n"
        "Выбери параметры:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_help(query):
    text = """❓ СПРАВКА

🎯 КАК Я РАБОТАЮ:

1️⃣ Отправь математическую задачу:
   ✍️ Текстом
   📸 Фото (я распознаю текст!)
2️⃣ Я задам наводящие вопросы
3️⃣ Отвечай и двигайся к решению
4️⃣ Когда будешь готов, нажми "Сдать ответ"
5️⃣ Я проверю твое решение и дам оценку

📸 ФОТО ЗАДАЧ:
Можешь сфотографировать задачу из учебника,
тетради или с доски - я распознаю текст!

📝 КОМАНДЫ:
/start - главное меню
/reset - начать новую задачу
/hint - попросить подсказку
/submit - сдать финальный ответ
/stats - посмотреть статистику
/keyboard - математические символы

🎓 РЕЖИМ ЭКЗАМЕНА:
Ограниченное количество подсказок
для самопроверки знаний

💪 Чем больше решаешь сам - 
тем лучше учишься!"""
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="start_solving")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def keyboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Базовые", callback_data='cat_basic'),
         InlineKeyboardButton("Греческие", callback_data='cat_greek')],
        [InlineKeyboardButton("Матанализ", callback_data='cat_calculus'),
         InlineKeyboardButton("Геометрия", callback_data='cat_geometry')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Выбери категорию символов:', reply_markup=reply_markup)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        session = get_session(user_id)
        
        if session.state not in [SessionState.WAITING_TASK, SessionState.SOLVING]:
            await update.message.reply_text(
                "❌ Сейчас я не жду фото. Используй /reset чтобы начать заново."
            )
            return
        
        await update.message.reply_text("📸 Получил фото! Распознаю текст задачи...")
        await update.message.chat.send_action("typing")
        
        photo = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)
        
        photo_bytes = io.BytesIO()
        await photo_file.download_to_memory(photo_bytes)
        photo_bytes.seek(0)
        
        image_data = base64.standard_b64encode(photo_bytes.read()).decode("utf-8")
        
        recognized_text = await recognize_math_from_image(image_data)
        
        if not recognized_text:
            await update.message.reply_text(
                "❌ Не удалось распознать задачу на фото.\n"
                "Попробуй:\n"
                "• Сфотографировать при хорошем освещении\n"
                "• Держать камеру ровно\n"
                "• Убедиться что текст четкий\n\n"
                "Или напиши задачу текстом."
            )
            return
        
        keyboard = [
            [InlineKeyboardButton("✅ Верно, решаем!", callback_data="confirm_task")],
            [InlineKeyboardButton("✏️ Исправить текст", callback_data="edit_task")],
            [InlineKeyboardButton("🔄 Другое фото", callback_data="retry_photo")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        session.temp_task = recognized_text
        
        await update.message.reply_text(
            f"📝 Я распознал такую задачу:\n\n"
            f"<code>{recognized_text}</code>\n\n"
            f"Все верно?",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        
    except Exception as e:
        error_text = f"""Ошибка в handle_photo:

**Текст ошибки:** {str(e)}

**User ID:** {update.effective_user.id}
**Username:** @{update.effective_user.username if update.effective_user.username else 'No username'}

**Трейсбек:**
```
{traceback.format_exc()}
```"""
        report_error(error_text)
        
        await update.message.reply_text(
            "😔 Не удалось обработать фото. Попробуй еще раз или напиши задачу текстом."
        )

async def recognize_math_from_image(image_base64):
    if not client:
        return None

    try:
        def _request():
            return client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": image_base64,
                                },
                            },
                            {
                                "type": "text",
                                "text": """Распознай математическую задачу с этого изображения.

ИНСТРУКЦИИ:
1. Извлеки ТОЛЬКО текст задачи (условие, вопрос)
2. Сохрани все математические символы, формулы, уравнения
3. Если есть несколько задач - извлеки все
4. Если это рукописный текст - постарайся распознать точно
5. Если на фото нет математической задачи - напиши "НЕТ ЗАДАЧИ"

ФОРМАТ ОТВЕТА:
Только чистый текст задачи, без комментариев и пояснений.

Примеры правильного формата:
- Реши уравнение: 2x + 5 = 15
- Найди производную функции f(x) = x³ - 2x + 1
- Упрости выражение: (a + b)² - (a - b)²"""
                        }
                    ],
                }
            ],
            )

        message = await asyncio.to_thread(_request)

        recognized_text = message.content[0].text.strip()
        
        if "НЕТ ЗАДАЧИ" in recognized_text.upper():
            return None
            
        return recognized_text
        
    except Exception as e:
        print(f"Ошибка распознавания: {e}")
        return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        user_message = update.message.text
        session = get_session(user_id)
        
        if session.state == SessionState.WAITING_TASK:
            session.current_task = user_message
            session.conversation = []
            session.state = SessionState.SOLVING
            session.task_start_time = datetime.now()
            session.stats["total_tasks"] += 1
            
            session.conversation.append({
                "role": "user",
                "content": f"Ученик хочет решить задачу: {user_message}\n\nНачни с проверки понимания условия задачи."
            })
            
            await update.message.chat.send_action("typing")
            
            response = await get_ai_response(session, SYSTEM_PROMPT)
            
            keyboard = [
                [InlineKeyboardButton("✅ Сдать ответ", callback_data="submit_answer")],
                [InlineKeyboardButton("💡 Подсказка", callback_data="hint")],
                [InlineKeyboardButton("🔄 Начать заново", callback_data="start_solving")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"📝 Задача принята!\n\n{response}",
                reply_markup=reply_markup
            )
            return
        
        if session.state == SessionState.SOLVING:
            session.conversation.append({
                "role": "user",
                "content": user_message
            })
            
            await update.message.chat.send_action("typing")
            
            response = await get_ai_response(session, SYSTEM_PROMPT)
            
            keyboard = [
                [InlineKeyboardButton("✅ Сдать ответ", callback_data="submit_answer")],
                [InlineKeyboardButton("💡 Подсказка", callback_data="hint")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(response, reply_markup=reply_markup)
            return
        
        if session.state == SessionState.FINAL_ANSWER:
            await update.message.chat.send_action("typing")
            await update.message.reply_text("🔍 Проверяю твое решение...")
            
            verification = await verify_solution(session.current_task, user_message)
            
            task_time = (datetime.now() - session.task_start_time).seconds // 60
            session.stats["completed_tasks"] += 1
            
            old_avg = session.stats["average_score"]
            new_score = verification["score"]
            total = session.stats["completed_tasks"]
            session.stats["average_score"] = (old_avg * (total - 1) + new_score) / total
            
            session.stats["tasks_history"].append({
                "date": datetime.now().strftime("%d.%m"),
                "score": new_score,
                "time": task_time,
                "task_preview": session.current_task[:30] + "..." if len(session.current_task) > 30 else session.current_task
            })
            
            await update.message.chat.send_action("typing")
            meme_text = await generate_meme_text(new_score, session.current_task, session.difficulty)
            
            emoji = "🎉" if new_score >= 80 else "👍" if new_score >= 60 else "💪"
            result_text = f"""{emoji} ПРОВЕРКА ЗАВЕРШЕНА

📝 Задача: {session.current_task[:50]}...

✅ Правильность: {"Верно!" if verification['correct'] else "Есть ошибки"}
⭐ Оценка: {new_score}/100
⏱ Время: {task_time} мин

📊 ОБРАТНАЯ СВЯЗЬ:
{verification['feedback']}

"""
            
            if verification.get('strengths'):
                result_text += "💪 ЧТО ХОРОШО:\n"
                for s in verification['strengths']:
                    result_text += f"• {s}\n"
                result_text += "\n"
            
            if verification.get('mistakes'):
                result_text += "⚠️ НАД ЧЕМ ПОРАБОТАТЬ:\n"
                for m in verification['mistakes']:
                    result_text += f"• {m}\n"
            
            if not verification['correct']:
                result_text += f"\n✏️ Правильный ответ: {verification['final_answer']}"
            
            keyboard = [
                [InlineKeyboardButton("📚 Новая задача", callback_data="start_solving")],
                [InlineKeyboardButton("📊 Статистика", callback_data="show_stats")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            session.state = SessionState.COMPLETED
            
            await update.message.reply_text(result_text, reply_markup=reply_markup)
            
            if session.meme_enabled:
                session.stats["total_memes_earned"] = session.stats.get("total_memes_earned", 0) + 1

                await update.message.chat.send_action("typing")
                await asyncio.sleep(1)

                meme_emoji = "🎯" if new_score >= 80 else "🙂" if new_score >= 60 else "😢"
                if meme_text:
                    await update.message.reply_text(f"{meme_emoji} {meme_text}")

            # Сбрасываем состояние после завершения задачи
            session.current_task = None
            session.conversation = []
            session.task_start_time = None
            session.state = SessionState.WAITING_TASK
            
    except Exception as e:
        error_text = f"""Ошибка в handle_message:

**Текст ошибки:** {str(e)}

**User ID:** {update.effective_user.id}
**Username:** @{update.effective_user.username if update.effective_user.username else 'No username'}
**Сообщение пользователя:** {update.message.text if update.message.text else 'Нет текста'}

**Трейсбек:**
```
{traceback.format_exc()}
```"""
        
        report_error(error_text)
        
        await update.message.reply_text(
            "😔 Произошла ошибка. Я уже сообщил разработчику!"
        )


def build_statistics_text(session: UserSession) -> str:
    stats = session.stats

    if stats["total_tasks"] == 0:
        return "📊 Статистика пока пуста.\n\nРеши несколько задач, чтобы увидеть свой прогресс!"

    success_rate = (stats["completed_tasks"] / stats["total_tasks"] * 100) if stats["total_tasks"] else 0

    text = (
        "📊 ТВОЯ СТАТИСТИКА\n\n"
        f"✅ Решено задач: {stats['completed_tasks']}/{stats['total_tasks']}\n"
        f"⭐ Средний балл: {stats['average_score']:.1f}/100\n"
        f"💡 Использовано подсказок: {stats['total_hints']}\n"
        f"🎭 Заработано мемов: {stats.get('total_memes_earned', 0)}\n"
        f"📈 Процент успеха: {success_rate:.1f}%\n\n"
        "📚 Последние задачи:"
    )

    if stats["tasks_history"]:
        for task in stats["tasks_history"][-5:]:
            emoji = "✅" if task["score"] >= 70 else "⚠️" if task["score"] >= 50 else "❌"
            text += f"\n{emoji} {task['date']}: {task['score']}/100"
    else:
        text += "\n— пока нет завершенных задач"

    return text


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    session.state = SessionState.WAITING_TASK
    session.current_task = None
    session.conversation = []
    session.task_start_time = None

    await update.message.reply_text(
        "🔄 Начнем заново! Отправь новую математическую задачу или фото."
    )


async def submit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)

    if session.state != SessionState.SOLVING:
        await update.message.reply_text("Сначала отправь задачу и начни решение.")
        return

    session.state = SessionState.FINAL_ANSWER
    await update.message.reply_text(
        "✍️ Отлично! Теперь напиши свой ФИНАЛЬНЫЙ ОТВЕТ на задачу.\n\n"
        "Постарайся расписать полный ход решения."
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    text = build_statistics_text(session)
    await update.message.reply_text(text)


async def hint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)

    if session.state != SessionState.SOLVING:
        await update.message.reply_text("Подсказки доступны только во время решения задачи.")
        return

    session.stats["total_hints"] += 1
    session.conversation.append({
        "role": "user",
        "content": "Мне нужна подсказка. Дай небольшую подсказку, но не решение."
    })

    await update.message.chat.send_action("typing")
    response = await get_ai_response(session, SYSTEM_PROMPT)
    await update.message.reply_text(f"💡 {response}")


async def call_claude(messages, system_prompt: str, max_tokens: int = 600) -> str:
    if not client:
        raise RuntimeError("Anthropic client is not configured")

    def _request():
        response = client.messages.create(
            model=DEFAULT_MODEL,
            system=system_prompt,
            messages=messages,
            max_tokens=max_tokens,
        )

        parts = []
        for block in getattr(response, "content", []) or []:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts).strip()

    return await asyncio.to_thread(_request)


async def get_ai_response(session: UserSession, system_prompt: str) -> str:
    # Преобразуем историю в формат Anthropic
    messages = []
    for turn in session.conversation:
        messages.append({
            "role": turn["role"],
            "content": [{"type": "text", "text": turn["content"]}]
        })

    try:
        if client:
            reply = await call_claude(messages, system_prompt)
        else:
            raise RuntimeError("Anthropic client unavailable")
    except Exception:
        # Фолбэк, чтобы бот продолжал работать даже без LLM
        reply = (
            "Давай подумаем вместе. Попробуй описать следующий шаг решения."
            if session.state == SessionState.SOLVING
            else "Расскажи подробнее, что именно тебя интересует в задаче."
        )

    session.conversation.append({"role": "assistant", "content": reply})
    return reply


async def verify_solution(task_text: str, student_answer: str) -> dict:
    prompt = VERIFICATION_PROMPT.format(
        original_task=task_text,
        student_answer=student_answer
    )

    if client:
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": prompt}]
        }]
        try:
            response = await call_claude(messages, "Ты строгий, но справедливый проверяющий.")
            data = json.loads(response)
            # Гарантируем обязательные поля
            return {
                "correct": bool(data.get("correct", False)),
                "final_answer": data.get("final_answer", "Не удалось определить"),
                "score": int(data.get("score", 0)),
                "feedback": data.get("feedback", "Нет обратной связи"),
                "mistakes": data.get("mistakes", []),
                "strengths": data.get("strengths", []),
            }
        except Exception:
            pass

    # Фолбэк, если LLM недоступен или ответ некорректен
    return {
        "correct": False,
        "final_answer": "н/д",
        "score": 50,
        "feedback": (
            "Пока не могу проверить решение автоматически."
            " Попробуй самостоятельно оценить ответ или повтори попытку позже."
        ),
        "mistakes": ["Проверка выполнена в офлайн-режиме"],
        "strengths": [],
    }


async def generate_meme_text(score: int, task_text: str, difficulty: str) -> str:
    if client:
        prompt = MEME_GENERATION_PROMPT.format(
            score=score,
            task_type="текстовая задача" if len(task_text) > 40 else "быстрый пример",
            difficulty=difficulty,
        )
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": prompt}]
        }]
        try:
            return await call_claude(messages, "Создай короткий и позитивный мем.")
        except Exception:
            pass

    # Фолбэк без LLM
    if score >= 80:
        return "Gigachad момент! Математика сама решается, когда ты рядом."
    if score >= 60:
        return "W-победа! Еще пару шагов — и станешь легендой алгебры."
    if score >= 40:
        return "Мы это засчитаем. Маленькие победы тоже считаются!"
    return "Это не провал, это монтаж тренировки. В следующий раз точно разнесешь!"


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("submit", submit_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("hint", hint_command))
    application.add_handler(CommandHandler("keyboard", keyboard_command))

    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


def create_application() -> Application:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not configured")

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    register_handlers(application)
    return application


def main():
    application = create_application()
    application.run_polling()


if __name__ == "__main__":
    main()

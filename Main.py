import os
import discord
from discord.ext import commands
import asyncio
import datetime
import random
import sqlite3
import json

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='+', intents=intents, help_command=None)

# Database setup
def init_db():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        xp INTEGER DEFAULT 0,
        level INTEGER DEFAULT 0,
        reputation INTEGER DEFAULT 0,
        last_rep_given TEXT,
        rep_count_today INTEGER DEFAULT 0,
        last_rep_reset DATE,
        immune_until TEXT,
        rep_cooldowns TEXT,
        recent_reports TEXT,
        join_time DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS rep_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_code TEXT,
        action TEXT,
        from_user INTEGER,
        to_user INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS voice_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        channel_id INTEGER,
        join_time DATETIME,
        leave_time DATETIME
    )''')
    
    conn.commit()
    conn.close()

init_db()

# XP requirements
def get_xp_requirement(level):
    xp_requirements = {
        1: 200, 2: 300, 3: 500, 4: 800,
        5: 1100, 6: 1600, 7: 2200, 8: 3000,
        9: 3000, 10: 4000, 11: 4500
    }
    if level in xp_requirements:
        return xp_requirements[level]
    elif level <= 11:
        return xp_requirements.get(level, 5000)
    else:
        return 5000 + (level - 12) * 500

# Trans factor system
def get_trans_factor(level):
    if level <= 4:
        return "Отрицательный"
    elif level <= 8:
        return "Негативный"
    elif level <= 13:
        return "Нейтральный"
    elif level <= 17:
        return "Положительный"
    else:
        return "Отлично"

def get_db_connection():
    return sqlite3.connect('bot.db')

async def update_level(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT xp, level FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    if not result:
        conn.close()
        return
    
    xp, current_level = result
    new_level = current_level
    
    # Calculate new level
    while True:
        xp_needed = get_xp_requirement(new_level + 1)
        if xp >= xp_needed:
            new_level += 1
        else:
            break
    
    if new_level != current_level:
        c.execute("UPDATE users SET level = ? WHERE user_id = ?", (new_level, user_id))
        
        # Reputation change on level up/down
        rep_change = 2 if new_level > current_level else -2
        c.execute("UPDATE users SET reputation = reputation + ? WHERE user_id = ?", (rep_change, user_id))
        
        conn.commit()
        
        user = bot.get_user(user_id)
        if user:
            trans_factor = get_trans_factor(new_level)
            change_type = "повышен" if new_level > current_level else "понижен"
            await user.send(f"🎉 Ваш уровень {change_type} до {new_level}! Транс фактор: {trans_factor}\n"
                           f"{'📈' if rep_change > 0 else '📉'} Репутация изменена на {rep_change}")
    
    conn.close()

@bot.event
async def on_ready():
    print(f'Бот {bot.user} запущен!')
    # Reset daily rep counts
    await reset_daily_rep_counts()

async def reset_daily_rep_counts():
    while True:
        await asyncio.sleep(3600)  # Check every hour
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET rep_count_today = 0 WHERE date(last_rep_reset) < date('now')")
        c.execute("UPDATE users SET last_rep_reset = date('now') WHERE last_rep_reset IS NULL OR date(last_rep_reset) < date('now')")
        conn.commit()
        conn.close()

@bot.event
async def on_member_join(member):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, join_time) VALUES (?, ?)", 
              (member.id, datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # Check if user exists
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.author.id,))
    
    # Check first minute bonus
    c.execute("SELECT join_time FROM users WHERE user_id = ?", (message.author.id,))
    result = c.fetchone()
    if result and result[0]:
        join_time = datetime.datetime.fromisoformat(result[0])
        time_diff = datetime.datetime.now() - join_time
        if time_diff < datetime.timedelta(minutes=1):
            c.execute("UPDATE users SET xp = xp + 10 WHERE user_id = ?", (message.author.id,))
            await message.author.send("🎁 Бонус 10 XP за активность в первую минуту!")
    
    # Add XP for message
    c.execute("UPDATE users SET xp = xp + 2 WHERE user_id = ?", (message.author.id,))
    conn.commit()
    conn.close()
    
    await update_level(message.author.id)
    await bot.process_commands(message)

@bot.event
async def on_voice_state_update(member, before, after):
    conn = get_db_connection()
    c = conn.cursor()
    
    # User joined voice channel
    if not before.channel and after.channel:
        c.execute("INSERT INTO voice_sessions (user_id, channel_id, join_time) VALUES (?, ?, ?)",
                  (member.id, after.channel.id, datetime.datetime.now().isoformat()))
    
    # User left voice channel
    elif before.channel and not after.channel:
        # Check if was kicked/banned
        c.execute("SELECT join_time FROM voice_sessions WHERE user_id = ? AND leave_time IS NULL ORDER BY id DESC LIMIT 1",
                  (member.id,))
        result = c.fetchone()
        
        if result:
            join_time = datetime.datetime.fromisoformat(result[0])
            time_in_channel = datetime.datetime.now() - join_time
            
            # If was in voice for less than 1 minute, consider it as kick/ban
            if time_in_channel < datetime.timedelta(minutes=1):
                c.execute("UPDATE users SET reputation = reputation - 1 WHERE user_id = ?", (member.id,))
                await member.send("📉 Репутация уменьшена за выход из голосового канала")
            
            # If was in voice for 2+ hours, give +rep
            elif time_in_channel >= datetime.timedelta(hours=2):
                c.execute("UPDATE users SET reputation = reputation + 1 WHERE user_id = ?", (member.id,))
                await member.send("💚 +1 репутация за активность в голосовом канале (2+ часа)")
        
        # Update voice session
        c.execute("UPDATE voice_sessions SET leave_time = ? WHERE user_id = ? AND leave_time IS NULL",
                  (datetime.datetime.now().isoformat(), member.id))
    
    conn.commit()
    conn.close()

@bot.event
async def on_member_update(before, after):
    if before.timed_out_until != after.timed_out_until and after.timed_out_until:
        # User got timed out
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET reputation = reputation - 1 WHERE user_id = ?", (after.id,))
        conn.commit()
        conn.close()
        
        await after.send("📉 Репутация уменьшена за тайм-аут")

# Reputation commands
@bot.command(aliases=['реп', '+реп', 'реп+', 'реп +'])
async def rep_plus(ctx, target: discord.Member = None):
    await handle_rep(ctx, target, 1)

@bot.command(aliases=['-реп', 'реп-', 'реп -'])
async def rep_minus(ctx, target: discord.Member = None):
    await handle_rep(ctx, target, -1)

async def handle_rep(ctx, target, change):
    if not target:
        if ctx.message.reference:
            try:
                target = ctx.message.reference.resolved.author
            except:
                await ctx.author.send("❌ Не удалось определить целевого пользователя")
                return
        else:
            await ctx.author.send("❌ Укажите участника или ответьте на его сообщение")
            return
    
    if target.id == ctx.author.id:
        await ctx.author.send("❌ Нельзя изменить репутацию себе")
        return
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # Check author level
    c.execute("SELECT level FROM users WHERE user_id = ?", (ctx.author.id,))
    author_result = c.fetchone()
    if not author_result or author_result[0] < 2:
        await ctx.author.send("❌ Необходим 2+ уровень для изменения репутации")
        conn.close()
        return
    
    # Check target immunity for negative rep
    if change < 0:
        c.execute("SELECT immune_until FROM users WHERE user_id = ?", (target.id,))
        immune_result = c.fetchone()
        if immune_result and immune_result[0]:
            immune_until = datetime.datetime.fromisoformat(immune_result[0])
            if datetime.datetime.now() < immune_until:
                await ctx.author.send("❌ Цель имеет иммунитет к негативной репутации")
                conn.close()
                return
    
    # Check cooldowns and limits
    c.execute("SELECT rep_cooldowns, rep_count_today, last_rep_reset FROM users WHERE user_id = ?", (ctx.author.id,))
    result = c.fetchone()
    
    rep_cooldowns = json.loads(result[0]) if result and result[0] else {}
    rep_count_today = result[1] if result else 0
    last_rep_reset = result[2] if result else None
    
    # Reset daily counter if needed
    if last_rep_reset and datetime.datetime.now().date() > datetime.datetime.fromisoformat(last_rep_reset).date():
        rep_count_today = 0
        c.execute("UPDATE users SET rep_count_today = 0, last_rep_reset = ? WHERE user_id = ?",
                  (datetime.datetime.now().isoformat(), ctx.author.id))
    
    now = datetime.datetime.now().isoformat()
    target_key = str(target.id)
    
    # Check 3-day cooldown
    if target_key in rep_cooldowns:
        last_rep = datetime.datetime.fromisoformat(rep_cooldowns[target_key])
        if datetime.datetime.now() - last_rep < datetime.timedelta(days=3):
            await ctx.author.send("❌ Нельзя изменять репутацию чаще чем раз в 3 дня для одного пользователя")
            conn.close()
            return
    
    # Check daily limit
    if rep_count_today >= 7:
        await ctx.author.send("❌ Лимит 7 репутационных изменений в день")
        conn.close()
        return
    
    # Update reputation
    if change > 0:
        c.execute("UPDATE users SET reputation = reputation + ? WHERE user_id = ?", (change, target.id))
        await target.send("💚 Ваша репутация анонимно повышена")
    else:
        c.execute("UPDATE users SET reputation = reputation + ? WHERE user_id = ?", (change, target.id))
        
        # Check for immunity trigger (5 negative reps in 20 minutes)
        c.execute("SELECT recent_reports FROM users WHERE user_id = ?", (target.id,))
        reports_result = c.fetchone()
        recent_reports = json.loads(reports_result[0]) if reports_result and reports_result[0] else []
        
        # Filter reports from last 20 minutes
        twenty_min_ago = datetime.datetime.now() - datetime.timedelta(minutes=20)
        recent_reports = [r for r in recent_reports if datetime.datetime.fromisoformat(r) > twenty_min_ago]
        
        # Add current report
        recent_reports.append(now)
        
        # If 5+ reports in 20 minutes, grant immunity
        if len(recent_reports) >= 5:
            immune_until = datetime.datetime.now() + datetime.timedelta(hours=5)
            c.execute("UPDATE users SET immune_until = ? WHERE user_id = ?", 
                      (immune_until.isoformat(), target.id))
        
        c.execute("UPDATE users SET recent_reports = ? WHERE user_id = ?", 
                  (json.dumps(recent_reports), target.id))
    
    # Update cooldowns and counters
    rep_cooldowns[target_key] = now
    c.execute("UPDATE users SET rep_count_today = rep_count_today + 1, rep_cooldowns = ?, last_rep_reset = ? WHERE user_id = ?",
              (json.dumps(rep_cooldowns), datetime.datetime.now().isoformat(), ctx.author.id))
    
    # Logging
    session_code = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=6))
    
    # Send to log channel if set
    log_channel_id = os.getenv("LOG_CHANNEL")
    if log_channel_id:
        try:
            log_channel = bot.get_channel(int(log_channel_id))
            if log_channel:
                action = "+rep" if change > 0 else "-rep"
                await log_channel.send(f"`{session_code}` {action} | {ctx.author} → {target}")
        except:
            pass
    
    # Log to database
    c.execute("INSERT INTO rep_logs (session_code, action, from_user, to_user) VALUES (?, ?, ?, ?)",
              (session_code, "plus" if change > 0 else "minus", ctx.author.id, target.id))
    
    await ctx.author.send(f"ttts: жалоба/похвала отправлена #{session_code}")
    
    try:
        await ctx.message.delete()
    except:
        pass
    
    conn.commit()
    conn.close()

@bot.command()
async def profile(ctx):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT xp, level, reputation FROM users WHERE user_id = ?", (ctx.author.id,))
    result = c.fetchone()
    
    if result:
        xp, level, rep = result
        trans_factor = get_trans_factor(level)
        next_level_xp = get_xp_requirement(level + 1)
        await ctx.author.send(
            f"👤 **Профиль** {ctx.author.display_name}\n"
            f"⭐ **Уровень**: {level}\n"
            f"📊 **XP**: {xp}/{next_level_xp}\n"
            f"💚 **Репутация**: {rep}\n"
            f"🎭 **Транс фактор**: {trans_factor}"
        )
    else:
        await ctx.author.send("❌ Профиль не найден")
    conn.close()

@bot.command()
async def help_bot(ctx):
    help_text = """
**🤖 Команды бота:**

**📊 Система уровней:**
- Получайте XP за сообщения
- +10 XP бонус в первую минуту после входа
- Уровни автоматически повышаются

**💚 Репутация:**
- `+реп @user` или ответ `+реп` - повысить репутацию
- `-реп @user` или ответ `-реп` - понизить репутацию
- **Требуется:** 2+ уровень
- **Лимиты:** 7 в день, кд 3 дня на одного человека

**👤 Профиль:**
- `+profile` - посмотреть свой профиль

**⚙️ Автоматические действия:**
- -1 реп за тайм-аут
- -1 реп за быстрый выход из войса  
- +1 реп за 2+ часа в войсе
    """
    await ctx.author.send(help_text)

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("❌ DISCORD_TOKEN не найден в переменных окружения!")
        exit(1)
    bot.run(token)

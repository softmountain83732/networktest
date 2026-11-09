
import os
import platform
import asyncio
import psutil
import speedtest

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


BOT_TOKEN = os.getenv("BOT_TOKEN")


# =========================
# CPU INFORMATION
# =========================

def get_cpu_info():
    process = psutil.Process()

    cpu_usage = process.cpu_percent(interval=1)
    cpu_count = psutil.cpu_count() or 1

    # Normalize against logical CPU count
    cpu_usage = min(100, cpu_usage / cpu_count * 100)

    return {
        "model": platform.processor() or platform.machine(),
        "usage": cpu_usage,
        "cores": cpu_count
    }


# =========================
# RAM INFORMATION
# =========================

def get_memory_info():

    # Heroku container memory (cgroup v2)
    paths = [
        (
            "/sys/fs/cgroup/memory.current",
            "/sys/fs/cgroup/memory.max"
        ),
        (
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        )
    ]

    for usage_path, limit_path in paths:
        try:
            with open(usage_path) as f:
                used = int(f.read().strip())

            with open(limit_path) as f:
                limit_text = f.read().strip()

            if limit_text == "max":
                continue

            total = int(limit_text)

            if 0 < total < 2**60:
                return {
                    "used": used / (1024 ** 2),
                    "total": total / (1024 ** 2),
                    "percent": used / total * 100
                }

        except (OSError, ValueError):
            continue

    # Fallback: current Python process memory
    process = psutil.Process()
    used = process.memory_info().rss

    return {
        "used": used / (1024 ** 2),
        "total": None,
        "percent": None
    }


# =========================
# INTERNET SPEED TEST
# =========================

def get_network_speed():

    try:
        st = speedtest.Speedtest(timeout=20)

        st.get_best_server()

        download = st.download() / 1_000_000
        upload = st.upload() / 1_000_000
        ping = st.results.ping

        return {
            "download": download,
            "upload": upload,
            "ping": ping
        }

    except Exception as e:
        return {"error": str(e)}


# =========================
# SYSTEM INFORMATION
# =========================

def get_system_info():

    return {
        "os": platform.system(),
        "version": platform.release(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "dyno": os.getenv("DYNO", "Unknown")
    }


# =========================
# START COMMAND
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    msg = await update.message.reply_text(
        "⏳ Heroku Server Monitoring...\n\n"
        "Checking CPU, RAM and Network Speed..."
    )

    cpu = await asyncio.to_thread(get_cpu_info)
    ram = await asyncio.to_thread(get_memory_info)
    system = get_system_info()

    network = await asyncio.to_thread(get_network_speed)

    if "error" in network:
        network_text = (
            "❌ Network Speed Test Failed\n"
            f"Reason: {network['error']}"
        )
    else:
        network_text = (
            "🌐 NETWORK SPEED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📥 Download: {network['download']:.2f} Mbps\n"
            f"📤 Upload: {network['upload']:.2f} Mbps\n"
            f"🏓 Ping: {network['ping']:.2f} ms"
        )

    if ram["total"] is not None:
        ram_text = (
            f"💾 RAM Used: {ram['used']:.2f} MB\n"
            f"💾 RAM Limit: {ram['total']:.2f} MB\n"
            f"📊 RAM Usage: {ram['percent']:.2f}%"
        )
    else:
        ram_text = (
            f"💾 Process RAM: {ram['used']:.2f} MB\n"
            "📊 RAM Limit: Not detected"
        )

    text = f"""
🖥 HEROKU SERVER MONITOR
━━━━━━━━━━━━━━━━━━━━

⚙️ CPU INFORMATION

🔹 Model: {cpu['model']}
🔹 CPU Usage: {cpu['usage']:.2f}%
🔹 Logical CPUs: {cpu['cores']}

💾 MEMORY INFORMATION

{ram_text}

🌐 NETWORK SPEED

{network_text}

🖥 SERVER INFORMATION

🔹 Platform: Heroku
🔹 Dyno Process: {system['dyno']}
🔹 OS: {system['os']}
🔹 Version: {system['version']}
🔹 Architecture: {system['architecture']}
🔹 Python: {system['python']}

━━━━━━━━━━━━━━━━━━━━
🤖 STATUS: ONLINE
"""

    await msg.edit_text(text)


# =========================
# MAIN
# =========================

def main():

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing!")

    app = Application.builder().token(
        BOT_TOKEN
    ).build()

    app.add_handler(CommandHandler("start", start))

    print("Heroku Monitor Bot Started!")

    app.run_polling()


if __name__ == "__main__":
    main()


import os
import sys
import time
import asyncio
import platform
import socket
import shutil
import html
import logging
import subprocess

import psutil
import speedtest

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def read_file(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except (OSError, IOError):
        return None


def format_bytes(value):
    if value is None:
        return "N/A"

    value = float(value)

    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024

    return f"{value:.2f} TB"


def format_speed(value):
    if value is None:
        return "N/A"

    return f"{value / 1_000_000:.2f} Mbps"


def get_cpu_model():
    # Linux / Heroku
    cpuinfo = read_file("/proc/cpuinfo")

    if cpuinfo:
        for line in cpuinfo.splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()

            if line.lower().startswith("hardware"):
                return line.split(":", 1)[1].strip()

    # Fallback
    return platform.processor() or "Unknown"


def get_cpu_frequency():
    try:
        freq = psutil.cpu_freq()

        if freq and freq.current:
            return f"{freq.current:.0f} MHz"

    except Exception:
        pass

    return "N/A"


# ============================================================
# HEROKU MEMORY LIMIT DETECTION
# ============================================================

def get_cgroup_memory():
    """
    Detect memory limit and usage from cgroup v2 or v1.
    Returns: (limit, usage)
    """

    # cgroup v2
    limit = read_file("/sys/fs/cgroup/memory.max")
    usage = read_file("/sys/fs/cgroup/memory.current")

    if limit and usage:
        try:
            if limit != "max":
                return int(limit), int(usage)
        except ValueError:
            pass

    # cgroup v1
    limit = read_file(
        "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    )
    usage = read_file(
        "/sys/fs/cgroup/memory/memory.usage_in_bytes"
    )

    if limit and usage:
        try:
            limit_value = int(limit)
            usage_value = int(usage)

            # Ignore unlimited / unrealistic values
            if limit_value < 10**15:
                return limit_value, usage_value

        except ValueError:
            pass

    return None, None


def get_memory_info():
    virtual = psutil.virtual_memory()

    cgroup_limit, cgroup_usage = get_cgroup_memory()

    if cgroup_limit and cgroup_usage is not None:
        total = cgroup_limit
        used = cgroup_usage
        available = max(total - used, 0)
        source = "Heroku cgroup limit"

    else:
        total = virtual.total
        used = virtual.used
        available = virtual.available
        source = "System memory"

    percent = (used / total * 100) if total else 0

    return {
        "total": total,
        "used": used,
        "available": available,
        "percent": percent,
        "source": source,
    }


# ============================================================
# CPU INFORMATION
# ============================================================

def get_cpu_info():
    logical = psutil.cpu_count(logical=True) or os.cpu_count() or 1
    physical = psutil.cpu_count(logical=False)

    # CPU utilization sampling
    cpu_usage = psutil.cpu_percent(interval=1)

    return {
        "model": get_cpu_model(),
        "usage": cpu_usage,
        "logical": logical,
        "physical": physical,
        "frequency": get_cpu_frequency(),
    }


# ============================================================
# STORAGE INFORMATION
# ============================================================

def get_storage_info():
    try:
        usage = shutil.disk_usage("/")

        total = usage.total
        used = usage.used
        free = usage.free

        percent = (used / total * 100) if total else 0

        return {
            "total": total,
            "used": used,
            "free": free,
            "percent": percent,
        }

    except Exception as e:
        logger.exception("Storage detection failed: %s", e)
        return None


# ============================================================
# NETWORK SPEED TEST
# ============================================================

def run_speedtest():
    """
    Run Ookla Speedtest through speedtest-cli.
    Returns download/upload in bits per second and ping in ms.
    """

    try:
        test = speedtest.Speedtest(timeout=20)

        test.get_best_server()

        download = test.download()
        upload = test.upload()

        ping = test.results.ping

        server = test.results.server

        return {
            "download": download,
            "upload": upload,
            "ping": ping,
            "server": server.get("name", "Unknown"),
            "country": server.get("country", "Unknown"),
            "sponsor": server.get("sponsor", "Unknown"),
            "error": None,
        }

    except Exception as e:
        logger.exception("Network speed test failed")

        return {
            "download": None,
            "upload": None,
            "ping": None,
            "server": None,
            "country": None,
            "sponsor": None,
            "error": str(e),
        }


# ============================================================
# SYSTEM INFORMATION
# ============================================================

def get_system_info():
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_version": platform.release(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "heroku_dyno": os.getenv("DYNO", "Not detected"),
        "heroku_app": os.getenv("HEROKU_APP_NAME", "Not available"),
    }


# ============================================================
# COLLECT ALL INFORMATION
# ============================================================

def collect_server_info():
    result = {}

    # CPU
    result["cpu"] = get_cpu_info()

    # RAM
    result["memory"] = get_memory_info()

    # Disk
    result["storage"] = get_storage_info()

    # System
    result["system"] = get_system_info()

    # Network
    result["network"] = run_speedtest()

    return result


# ============================================================
# REPORT GENERATOR
# ============================================================

def generate_report(data):
    cpu = data["cpu"]
    memory = data["memory"]
    storage = data["storage"]
    network = data["network"]
    system = data["system"]

    physical_cores = (
        str(cpu["physical"])
        if cpu["physical"] is not None
        else "Unavailable"
    )

    report = []

    report.append("<b>HEROKU SERVER MONITOR</b>")
    report.append("<code>━━━━━━━━━━━━━━━━━━━━</code>")

    # NETWORK
    report.append("\n<b>NETWORK PERFORMANCE</b>")

    if network["error"]:
        report.append("Status: <b>Failed</b>")
        report.append(
            f"Error: <code>{html.escape(network['error'][:250])}</code>"
        )
    else:
        report.append(
            f"Download: <b>{format_speed(network['download'])}</b>"
        )
        report.append(
            f"Upload: <b>{format_speed(network['upload'])}</b>"
        )
        report.append(
            f"Ping: <b>{network['ping']:.2f} ms</b>"
        )
        report.append(
            f"Server: {html.escape(str(network['server']))}, "
            f"{html.escape(str(network['country']))}"
        )
        report.append(
            f"Provider: {html.escape(str(network['sponsor']))}"
        )

    # CPU
    report.append("\n<b>CPU INFORMATION</b>")
    report.append(
        f"Model: <code>{html.escape(cpu['model'])}</code>"
    )
    report.append(
        f"Usage: <b>{cpu['usage']:.1f}%</b>"
    )
    report.append(
        f"Logical Cores: <b>{cpu['logical']}</b>"
    )
    report.append(
        f"Physical Cores: <b>{physical_cores}</b>"
    )
    report.append(
        f"Frequency: <b>{cpu['frequency']}</b>"
    )

    # MEMORY
    report.append("\n<b>RAM INFORMATION</b>")
    report.append(
        f"Total: <b>{format_bytes(memory['total'])}</b>"
    )
    report.append(
        f"Used: <b>{format_bytes(memory['used'])}</b>"
    )
    report.append(
        f"Available: <b>{format_bytes(memory['available'])}</b>"
    )
    report.append(
        f"Usage: <b>{memory['percent']:.1f}%</b>"
    )
    report.append(
        f"Source: {html.escape(memory['source'])}"
    )

    # STORAGE
    report.append("\n<b>DISK / SSD INFORMATION</b>")

    if storage:
        report.append(
            f"Total: <b>{format_bytes(storage['total'])}</b>"
        )
        report.append(
            f"Used: <b>{format_bytes(storage['used'])}</b>"
        )
        report.append(
            f"Free: <b>{format_bytes(storage['free'])}</b>"
        )
        report.append(
            f"Usage: <b>{storage['percent']:.1f}%</b>"
        )
        report.append(
            "<i>Filesystem visible to the Heroku dyno.</i>"
        )
    else:
        report.append("Storage information unavailable.")

    # SYSTEM
    report.append("\n<b>SYSTEM INFORMATION</b>")
    report.append(
        f"Hostname: <code>{html.escape(system['hostname'])}</code>"
    )
    report.append(
        f"OS: {html.escape(system['os'])} "
        f"{html.escape(system['os_version'])}"
    )
    report.append(
        f"Architecture: {html.escape(system['architecture'])}"
    )
    report.append(
        f"Python: {html.escape(system['python'])}"
    )
    report.append(
        f"Dyno: <code>{html.escape(system['heroku_dyno'])}</code>"
    )
    report.append(
        f"Heroku App: {html.escape(system['heroku_app'])}"
    )

    report.append("\n<code>━━━━━━━━━━━━━━━━━━━━</code>")
    report.append("<i>Powered by Heroku Server Monitor</i>")

    return "\n".join(report)


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Heroku Server Monitor.\n\n"
        "Use /test to check server performance."
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_message = await update.message.reply_text(
        "⏳ <b>Running Heroku server diagnostics...</b>\n\n"
        "Checking CPU, RAM, Storage and Network speed.\n"
        "This may take 30-60 seconds.",
        parse_mode=ParseMode.HTML,
    )

    try:
        # Run blocking system checks and speedtest outside event loop
        data = await asyncio.to_thread(collect_server_info)

        report = generate_report(data)

        await status_message.edit_text(
            report,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception as e:
        logger.exception("Server diagnostics failed")

        await status_message.edit_text(
            "❌ <b>Diagnostics failed</b>\n\n"
            f"<code>{html.escape(str(e)[:1000])}</code>",
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Set BOT_TOKEN in Heroku Config Vars."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test))

    logger.info("Heroku Server Monitor started.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()

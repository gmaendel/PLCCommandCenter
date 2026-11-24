import time
import datetime
import subprocess
import os
import sqlite3
import json
from pycomm3 import SLCDriver
import requests
import plccc_settings as settings  # project paths + secrets from .env

# Configuration
PLC_HOST = settings.PUMP_PLC_HOST
PLC_PORT = settings.PUMP_PLC_PORT
PLC_IP = f'{PLC_HOST}:{PLC_PORT}'  # pycomm3 supports host:port path format
PLC_TAG = 'N7:6'
PLC_TAG_LEVEL = 'N7:1'
THRESHOLD = 2000  # Default fallback
CONFIG_PATH = settings.CONFIG_PATH

def gpm_alarm_enabled(conn):
    """Honor the dashboard's alarm on/off switch for the 'Pump GPM' tag."""
    try:
        row = conn.execute("SELECT alarm_enabled FROM tags WHERE name='Pump GPM'").fetchone()
        return True if row is None else bool(row[0])
    except Exception:
        return True

def get_threshold():
    """Read the current alarm threshold from the shared config file."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
            return int(cfg.get('threshold_gpm', THRESHOLD))
    except Exception:
        return THRESHOLD

TELEGRAM_TARGET = settings.TELEGRAM_CHAT_ID
TELEGRAM_BOT_TOKEN = settings.TELEGRAM_BOT_TOKEN
ACK_FILE = '/tmp/pump_ack'

# iMessage Group Chat via AppleScript

POLL_INTERVAL = 60          # 1 minute
ESCALATION_WAIT = 400       # 6.6 minutes
COOLDOWN_TIME = 120         # 2 minutes

def send_telegram_message(text):
    """Sends a message directly via Telegram Bot API."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_TARGET, "text": text}
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"[{datetime.datetime.now()}] Sent Telegram Alert: {text}")
        else:
            print(f"[{datetime.datetime.now()}] Telegram API error {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"Exception sending Telegram message: {e}")

def send_sms_message(text):
    """Sends an iMessage to the Recharge Pump Alarm group chat via AppleScript."""
    safe_text = text.replace('\\', '\\\\').replace('"', '\\"')
    apple_script = f'''
    tell application "Messages"
        try
            set targetChat to first chat whose name is "Recharge Pump Alarm"
            send "{safe_text}" to targetChat
        on error errMsg number errNum
            error errMsg number errNum
        end try
    end tell
    '''

    def _run_and_check():
        try:
            result = subprocess.run(
                ["osascript", "-e", apple_script],
                capture_output=True, text=True, timeout=25
            )
            with open('/tmp/imessage_background.log', 'a') as f:
                f.write(f"[{datetime.datetime.now()}] exit={result.returncode} stdout={result.stdout.strip()} stderr={result.stderr.strip()}\n")
            if result.returncode != 0:
                err = result.stderr.strip() or result.stdout.strip() or "unknown error"
                print(f"[{datetime.datetime.now()}] iMessage FAILED (exit {result.returncode}): {err}")
                send_telegram_message(f"⚠️ iMessage send FAILED!\nError: {err}")
            else:
                print(f"[{datetime.datetime.now()}] iMessage Group Alert sent successfully.")
        except subprocess.TimeoutExpired:
            print(f"[{datetime.datetime.now()}] iMessage send TIMED OUT (>25s) — Messages.app may be hung.")
            send_telegram_message("⚠️ iMessage send TIMED OUT (>25s) — Messages.app may be hung.")
        except Exception as e:
            print(f"[{datetime.datetime.now()}] iMessage exception: {e}")
            send_telegram_message(f"⚠️ iMessage send exception: {e}")

    try:
        import threading
        t = threading.Thread(target=_run_and_check, daemon=True)
        t.start()
        print(f"[{datetime.datetime.now()}] Dispatched iMessage Group Alert to background thread.")
    except Exception as e:
        print(f"Exception sending iMessage: {e}")

import socket

def check_imessage_for_ack(since_timestamp=None):
    """Checks the local macOS chat.db to see if any user replied 'ack' in the group chat recently.
    
    since_timestamp: Unix timestamp (float). If provided, only messages after this time are checked.
    Uses case-insensitive display_name matching to handle name casing differences.
    """
    db_path = os.path.expanduser("~/Library/Messages/chat.db")
    if not os.path.exists(db_path):
        return False
        
    try:
        # Apple stores message.date as nanoseconds since 2001-01-01
        # Convert since_timestamp (Unix epoch) to Apple epoch nanoseconds
        time_filter = ""
        if since_timestamp is not None:
            apple_epoch_offset = 978307200  # seconds between Unix epoch and Apple epoch (2001-01-01)
            apple_ns = int((since_timestamp - apple_epoch_offset) * 1e9)
            time_filter = f"AND message.date > {apple_ns}"

        query = f"""
        SELECT message.text
        FROM message 
        JOIN chat_message_join ON message.ROWID = chat_message_join.message_id
        JOIN chat ON chat_message_join.chat_id = chat.ROWID
        WHERE LOWER(chat.display_name) = LOWER('Recharge Pump Alarm')
        AND message.is_from_me = 0
        {time_filter}
        ORDER BY message.date DESC LIMIT 10;
        """
        res = subprocess.run(["sqlite3", db_path, query], capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.strip().split('\n'):
                if not line: continue
                text = line.lower().strip()
                if "ack" in text or "got it" in text:
                    return True
    except Exception as e:
        print(f"Exception checking chat.db for ack: {e}")
        
    return False

def ping_plc(host, port=PLC_PORT):
    """Checks if the PLC port is open instead of using ICMP ping, since ICMP is blocked."""
    try:
        ip = socket.gethostbyname(host)
        with socket.create_connection((ip, port), timeout=3):
            return True
    except Exception:
        return False

def main():
    print(f"Starting MicroLogix Monitor for IP {PLC_IP} | Tag: {PLC_TAG}")
    
    alert_active = False
    alert_stage = 0
    offline_alert_active = False
    last_escalation_time = 0
    alarm_start_time = 0
    fail_count = 0
    
    # Ensure ACK file doesn't exist on startup
    if os.path.exists(ACK_FILE):
        os.remove(ACK_FILE)
        
    # Setup SQLite Database for Web Dashboard
    db_path = settings.DB_PATH
    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute('''CREATE TABLE IF NOT EXISTS flow_log (timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, gpm REAL, level REAL)''')
        # Safe migration: add level column if it doesn't exist (won't affect existing data)
        try:
            conn.execute("ALTER TABLE flow_log ADD COLUMN level REAL")
        except sqlite3.OperationalError:
            pass  # Column already exists, that's fine
    except Exception as e:
        print(f"Failed to initialize SQLite database: {e}")
        conn = None
    
    comm = SLCDriver(PLC_IP)
    comm.open()
    
    while True:
        current_time = time.time()
        
        # 1. Read from the PLC
        try:
            if not comm.connected:
                comm.open()
                
            # Read both tags in a single request to prevent transaction ID desync on timeouts
            results = comm.read(PLC_TAG, PLC_TAG_LEVEL)
            if not results or len(results) != 2 or results[0].error or results[1].error:
                print(f"[{datetime.datetime.now()}] Failed to read tags from {PLC_IP}.")
                fail_count += 1
                # If we get a read error, force close the socket so it rebuilds cleanly
                try: comm.close()
                except: pass
            else:
                gpm = results[0].value
                level_val = results[1].value
                fail_count = 0
                
                # Log to SQLite DB
                if conn:
                    try:
                        # Log the value
                        conn.execute("INSERT INTO flow_log (gpm, level) VALUES (?, ?)", (gpm, level_val))
                        # Cleanup: keep only last 60 days of data
                        conn.execute("DELETE FROM flow_log WHERE timestamp < datetime('now', '-60 days')")
                    except Exception as e:
                        print(f"DB Insert Error: {e}")
                
                if offline_alert_active:
                    offline_alert_active = False
                    msg = f"✅ RECOVERY: Communication to PLC {PLC_IP} has been restored!"
                    send_telegram_message(msg)
                    send_sms_message(msg)
                    
                timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{timestamp_str}] Read GPM: {gpm} | Level: {level_val}")
                
                # 2. Logic Evaluation
                threshold = get_threshold()
                if gpm < threshold and gpm_alarm_enabled(conn):
                    if not alert_active:
                        # Trigger New Alert
                        alert_active = True
                        alert_stage = 1
                        
                        if os.path.exists(ACK_FILE):
                            os.remove(ACK_FILE)
                        
                        msg = f"⚠️ ALARM: Recharge Pump GPM is below {threshold} GPM!\nCurrent GPM: {gpm}\nTime: {timestamp_str}\n\n(Reply 'ack' to silence further alerts)"
                        send_telegram_message(msg)
                        send_sms_message(msg)
                        last_escalation_time = current_time
                        alarm_start_time = current_time  # Track when this alarm cycle began
                        
                    else:
                        time_since_escalation = current_time - last_escalation_time
                        
                        # Check for acknowledgment from user (via Opal writing to ACK_FILE)
                        # OR check iMessage database directly (only messages since alarm started)
                        user_acked = False
                        if os.path.exists(ACK_FILE):
                            user_acked = True
                        elif check_imessage_for_ack(since_timestamp=alarm_start_time):
                            user_acked = True
                            # Touch the ACK file so Telegram side knows it's acked too
                            open(ACK_FILE, 'a').close()
                            
                        if user_acked:
                            if alert_stage != 4:
                                print(f"[{datetime.datetime.now()}] Alarm acknowledged. Silencing further escalations.")
                                alert_stage = 4 
                        
                        if alert_stage == 1 and time_since_escalation >= ESCALATION_WAIT:
                            msg = f"⚠️ ESCALATION 1: Recharge Pump still below {threshold}!\nCurrent GPM: {gpm}\nTime: {timestamp_str}"
                            send_telegram_message(msg)
                            send_sms_message(msg)
                            alert_stage = 2
                            last_escalation_time = current_time
                            
                        elif alert_stage == 2 and time_since_escalation >= ESCALATION_WAIT:
                            msg = f"🚨 FINAL WARNING: Recharge Pump still below {threshold}!\nCurrent GPM: {gpm}\nTime: {timestamp_str}"
                            send_telegram_message(msg)
                            send_sms_message(msg)
                            alert_stage = 3
                            last_escalation_time = current_time
                            
                else:
                    # GPM is normal
                    if alert_active:
                        print(f"[{timestamp_str}] GPM normalized ({gpm}). Entering 2-minute cooldown...")
                        time.sleep(COOLDOWN_TIME)
                        alert_active = False
                        alert_stage = 0
                        if os.path.exists(ACK_FILE):
                            os.remove(ACK_FILE)
                        print("Cooldown complete. Monitoring re-armed.")
                        
        except Exception as e:
            print(f"[{datetime.datetime.now()}] Exception reading {PLC_TAG} from {PLC_IP}: {e}")
            fail_count += 1
            # Force close socket on exception so it doesn't desync on next loop
            try: comm.close()
            except: pass
            
        if fail_count >= 10:
            print(f"[{datetime.datetime.now()}] {fail_count} consecutive read failures. Pinging PLC...")
            if ping_plc(PLC_HOST):
                print(f"[{datetime.datetime.now()}] Ping successful! Driver seems wedged. Restarting driver...")
                try:
                    comm.close()
                except Exception:
                    pass
                comm = SLCDriver(PLC_IP)
                comm.open()
                fail_count = 0  # Reset fail count after recreating driver
            else:
                print(f"[{datetime.datetime.now()}] Ping failed. PLC might be offline. Retrying normally...")
                if not offline_alert_active:
                    offline_alert_active = True
                    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    msg = f"🚨 COMM LOSS: Cannot reach PLC at {PLC_HOST}:{PLC_PORT}.\nTime: {timestamp_str}\n\nMonitoring will resume automatically when it comes back online."
                    send_telegram_message(msg)
                    send_sms_message(msg)
                # We leave fail_count at 10 so it pings again next interval
                
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()

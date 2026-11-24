import sqlite3
import json
import os
from flask import Flask, jsonify, render_template_string, request
import datetime
import zoneinfo
import plccc_settings as settings  # project paths + secrets from .env

CONFIG_PATH = settings.CONFIG_PATH
DB_PATH     = settings.DB_PATH
THRESHOLD_PASSWORD = settings.DASHBOARD_PASSWORD


def get_threshold():
    try:
        with open(CONFIG_PATH, 'r') as f:
            return int(json.load(f).get('threshold_gpm', 2000))
    except Exception:
        return 2000


def set_threshold(value):
    try:
        cfg = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
        cfg['threshold_gpm'] = int(value)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception:
        return False


def init_db():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)

    # ── Existing tables ─────────────────────────────────────────────────────
    conn.execute('''CREATE TABLE IF NOT EXISTS flow_log (
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, gpm REAL, level REAL)''')
    try:
        conn.execute("ALTER TABLE flow_log ADD COLUMN level REAL")
    except Exception:
        pass

    conn.execute('''CREATE TABLE IF NOT EXISTS aquifer_log (
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, level REAL)''')

    # ── Device / tag config ─────────────────────────────────────────────────
    conn.execute('''CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, type TEXT NOT NULL,
        host TEXT DEFAULT '', port INTEGER DEFAULT 0, rack INTEGER DEFAULT 0,
        slot INTEGER DEFAULT 1, unit_id INTEGER DEFAULT 1, word_swap INTEGER DEFAULT 0,
        description TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT, device_id INTEGER NOT NULL,
        name TEXT NOT NULL, address TEXT NOT NULL, data_type TEXT DEFAULT 'REAL',
        units TEXT DEFAULT '', description TEXT DEFAULT '',
        alarm_enabled INTEGER DEFAULT 0, alarm_low REAL DEFAULT NULL,
        alarm_high REAL DEFAULT NULL, alarm_message TEXT DEFAULT '',
        scale_factor REAL DEFAULT 1.0, scale_offset REAL DEFAULT 0.0,
        enabled INTEGER DEFAULT 1, log_source TEXT DEFAULT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE)''')

    # Migrations for existing columns
    for col, defn in [
        ('log_source',    'TEXT DEFAULT NULL'),
        ('word_swap',     'INTEGER DEFAULT 0'),
        ('alarm_enabled', 'INTEGER DEFAULT 0'),
        ('alarm_low',     'REAL DEFAULT NULL'),
        ('alarm_high',    'REAL DEFAULT NULL'),
        ('alarm_message', "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE tags ADD COLUMN {col} {defn}")
        except Exception:
            pass
        try:
            conn.execute(f"ALTER TABLE devices ADD COLUMN {col} {defn}")
        except Exception:
            pass

    # Scaling columns (tags only)
    for col, defn in [
        ('scale_factor', 'REAL DEFAULT 1.0'),
        ('scale_offset', 'REAL DEFAULT 0.0'),
    ]:
        try:
            conn.execute(f"ALTER TABLE tags ADD COLUMN {col} {defn}")
        except Exception:
            pass

    # ── Tag readings (generic last-value store) ─────────────────────────────
    conn.execute('''CREATE TABLE IF NOT EXISTS tag_readings (
        tag_id INTEGER NOT NULL, value REAL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE)''')

    # ── Alarm state (written by monitors, read by web UI) ───────────────────
    conn.execute('''CREATE TABLE IF NOT EXISTS tag_alarm_state (
        tag_id INTEGER PRIMARY KEY, active INTEGER DEFAULT 0,
        triggered_at DATETIME DEFAULT NULL, last_value REAL DEFAULT NULL,
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE)''')

    # ── Seed on first run ────────────────────────────────────────────────────
    count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    if count == 0:
        conn.execute("""INSERT INTO devices (name,type,host,port,description) VALUES
            ('Recharge Pump (MicroLogix)','allen_bradley_slc','pump-plc.example.com',44818,
             'Allen-Bradley MicroLogix – recharge pump flow monitor')""")
        mlx_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.executemany(
            "INSERT INTO tags (device_id,name,address,data_type,units,description,log_source) VALUES (?,?,?,?,?,?,?)",
            [(mlx_id,'Pump GPM','N7:6','INT','GPM','Recharge pump flow rate','{"table":"flow_log","column":"gpm"}'),
             (mlx_id,'Water Level','N7:1','INT','','Water level sensor reading','{"table":"flow_log","column":"level"}')])
        conn.execute("""INSERT INTO devices (name,type,host,port,rack,slot,description) VALUES
            ('Aquifer Monitor (S7-1200)','siemens_s7','pump-plc.example.com',102,0,1,
             'Siemens S7-1200 – aquifer level sensor')""")
        s7_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO tags (device_id,name,address,data_type,units,description,log_source) VALUES (?,?,?,?,?,?,?)",
            (s7_id,'Aquifer Level','DB6.REAL108','REAL','ft','Aquifer depth measurement',
             '{"table":"aquifer_log","column":"level"}'))

    # Backfill log_source for seed tags
    conn.execute("UPDATE tags SET log_source='{\"table\":\"flow_log\",\"column\":\"gpm\"}' WHERE address='N7:6' AND log_source IS NULL")
    conn.execute("UPDATE tags SET log_source='{\"table\":\"flow_log\",\"column\":\"level\"}' WHERE address='N7:1' AND log_source IS NULL")
    conn.execute("UPDATE tags SET log_source='{\"table\":\"aquifer_log\",\"column\":\"level\"}' WHERE address='DB6.REAL108' AND log_source IS NULL")

    conn.close()


init_db()
app = Flask(__name__)


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>⚡ PLC Command Center</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/moment@2.29.4/moment.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-moment@1.0.1"></script>
    <script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
    <style>
        body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; background:#f4f7fa; color:#333; margin:0; padding:20px; }
        .container { max-width:1150px; margin:0 auto; background:#fff; padding:24px; border-radius:8px; box-shadow:0 4px 6px rgba(0,0,0,0.1); }
        h1 { text-align:center; color:#2c3e50; }
        .tabs { display:flex; justify-content:center; margin-bottom:20px; border-bottom:2px solid #ecf0f1; flex-wrap:wrap; }
        .tab-btn { background:none; border:none; padding:10px 22px; font-size:1.05em; cursor:pointer; color:#7f8c8d; font-weight:bold; }
        .tab-btn:hover { color:#2980b9; }
        .tab-btn.active { color:#2980b9; border-bottom:3px solid #2980b9; margin-bottom:-2px; }
        .tab-content { display:none; }
        .tab-content.active { display:block; }
        .status-card { text-align:center; padding:20px; margin-bottom:20px; border-radius:8px; background:#e8f4f8; border-left:5px solid #3498db; display:flex; flex-direction:column; align-items:center; }
        .status-card h2 { margin:0; font-size:2.5em; color:#2980b9; display:flex; align-items:center; }
        .status-card p { margin:5px 0 0; color:#7f8c8d; }
        .led { display:inline-block; width:20px; height:20px; border-radius:50%; background:#bdc3c7; margin-right:15px; box-shadow:inset 0 2px 4px rgba(0,0,0,0.2); transition:background-color 0.3s,box-shadow 0.3s; }
        .led.green { background:#2ecc71; box-shadow:0 0 12px #2ecc71,inset 0 1px 2px rgba(255,255,255,0.5); }
        .led.red   { background:#e74c3c; box-shadow:0 0 12px #e74c3c,inset 0 1px 2px rgba(255,255,255,0.5); }
        .chart-container { position:relative; height:400px; width:100%; }
        .controls { text-align:center; margin-bottom:20px; }
        .controls button { background:#3498db; color:white; border:none; padding:10px 20px; margin:5px; border-radius:5px; cursor:pointer; font-size:1em; }
        .controls button:hover { background:#2980b9; }
        .controls button.active { background:#2c3e50; }
        .custom-range { margin-top:10px; display:flex; flex-wrap:wrap; justify-content:center; align-items:center; gap:10px; }
        .custom-range input { padding:8px; border:1px solid #bdc3c7; border-radius:4px; max-width:100%; box-sizing:border-box; }
        .threshold-card { display:flex; align-items:center; justify-content:center; gap:12px; margin-bottom:16px; padding:12px 20px; background:#fef9e7; border-left:5px solid #f39c12; border-radius:8px; flex-wrap:wrap; }
        .threshold-card label { font-weight:bold; color:#7f8c8d; }
        .threshold-card input[type=number] { width:100px; padding:6px 10px; border:1px solid #f39c12; border-radius:4px; font-size:1em; text-align:center; }
        .threshold-card button { background:#f39c12; color:white; border:none; padding:7px 18px; border-radius:5px; cursor:pointer; font-size:0.95em; font-weight:bold; }
        .threshold-card button:hover { background:#e67e22; }
        #thresholdStatus { font-size:0.9em; color:#27ae60; }

        /* ── Devices tab ── */
        .data-table { width:100%; border-collapse:collapse; font-size:0.9em; }
        .data-table th { background:#2c3e50; color:white; padding:10px 12px; text-align:left; font-weight:600; white-space:nowrap; }
        .data-table td { padding:9px 12px; border-bottom:1px solid #ecf0f1; vertical-align:middle; }
        .data-table tr:hover td { background:#f0f4f8; }
        .data-table tr.selected td { background:#d6eaf8; }
        .tag-code { background:#f0f0f0; padding:2px 7px; border-radius:3px; font-family:monospace; font-size:0.95em; }
        .badge { display:inline-block; padding:2px 9px; border-radius:12px; font-size:0.76em; font-weight:bold; color:white; white-space:nowrap; }
        .badge-ab-slc{background:#2980b9;} .badge-ab-clx{background:#1a5276;} .badge-siemens{background:#27ae60;} .badge-modbus{background:#e67e22;} .badge-unknown{background:#95a5a6;}

        /* ── Alarm badge ── */
        .alarm-badge { display:inline-block; padding:2px 7px; border-radius:10px; font-size:0.78em; font-weight:700; white-space:nowrap; }
        .alarm-on    { background:#fdebd0; color:#e67e22; border:1px solid #f0a500; }
        .alarm-active{ background:#fadbd8; color:#c0392b; border:1px solid #e74c3c; animation:pulse 1.2s infinite; }
        @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.6;} }

        /* ── Buttons ── */
        .btn-add{background:#27ae60;color:white;border:none;padding:8px 16px;border-radius:5px;cursor:pointer;font-size:0.92em;font-weight:bold;}
        .btn-add:hover{background:#229954;}
        .btn-edit{background:#3498db;color:white;border:none;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:0.82em;margin-right:4px;}
        .btn-edit:hover{background:#2980b9;}
        .btn-del{background:#e74c3c;color:white;border:none;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:0.82em;}
        .btn-del:hover{background:#c0392b;}
        .btn-save{background:#27ae60;color:white;border:none;padding:9px 22px;border-radius:5px;cursor:pointer;font-weight:bold;font-size:0.95em;}
        .btn-save:hover{background:#229954;}
        .btn-cancel{background:#bdc3c7;color:#2c3e50;border:none;padding:9px 22px;border-radius:5px;cursor:pointer;font-size:0.95em;}
        .btn-cancel:hover{background:#aab7b8;}

        /* ── Modals ── */
        .modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:1000;display:flex;align-items:center;justify-content:center;}
        .modal-box{background:white;border-radius:10px;padding:28px;width:520px;max-width:96vw;max-height:90vh;overflow-y:auto;box-shadow:0 12px 40px rgba(0,0,0,0.25);}
        .modal-box h3{margin:0 0 20px 0;color:#2c3e50;font-size:1.25em;}
        .form-row{display:flex;flex-direction:column;margin-bottom:13px;}
        .form-row label{font-weight:600;color:#555;font-size:0.86em;margin-bottom:4px;}
        .form-row input[type=text],.form-row input[type=number],.form-row input[type=password],.form-row select{padding:8px 10px;border:1px solid #bdc3c7;border-radius:4px;font-size:0.95em;width:100%;box-sizing:border-box;}
        .form-row input:focus,.form-row select:focus{outline:none;border-color:#3498db;}
        .form-row small{color:#7f8c8d;font-size:0.8em;margin-top:3px;}
        .form-inline{flex-direction:row!important;align-items:center!important;gap:10px;}
        .form-inline label{margin:0!important;}
        .form-inline input[type=checkbox]{width:auto!important;margin:0;}
        .form-grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
        .alarm-divider{border:none;border-top:1px solid #ecf0f1;margin:16px 0 14px;}
        .alarm-section-title{font-size:0.9em;font-weight:700;color:#e67e22;margin-bottom:12px;}
        .section-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;}
        .section-header h3{margin:0;color:#2c3e50;}
        .tag-section-wrap{margin-top:28px;border-top:2px solid #ecf0f1;padding-top:22px;}

        @media(max-width:600px){.controls button{width:100%;margin:5px 0;box-sizing:border-box;}.custom-range{flex-direction:column;align-items:stretch;gap:5px;}.data-table{font-size:0.8em;}.form-grid2{grid-template-columns:1fr;}}
    </style>
</head>
<body>
<div class="container">
    <h1>⚡ PLC Command Center</h1>
    <div class="tabs">
        <button class="tab-btn active" data-tab="pump" onclick="switchTab('pump')">Recharge Pump</button>
        <button class="tab-btn" data-tab="aquifer" onclick="switchTab('aquifer')">Aquifer Level</button>
        <button class="tab-btn" data-tab="devices" onclick="switchTab('devices')">⚙️ Devices</button>
    </div>

    <!-- PUMP TAB -->
    <div id="pump-tab" class="tab-content active">
        <div class="threshold-card">
            <label>⚠️ Alarm Threshold:</label>
            <input type="number" id="thresholdInput" value="2000" min="0" step="100">
            <span style="color:#7f8c8d;">GPM</span>
            <input type="password" id="thresholdPassword" placeholder="Password" style="width:90px;padding:6px 10px;border:1px solid #f39c12;border-radius:4px;font-size:1em;">
            <button onclick="saveThreshold()">Save</button>
            <span id="thresholdStatus"></span>
        </div>
        <div class="status-card">
            <div style="display:flex;justify-content:space-between;width:100%;max-width:600px;margin-bottom:15px;">
                <div style="text-align:left;"><div style="font-weight:bold;color:#7f8c8d;margin-bottom:5px;">MicroLogix PLC</div>
                <div style="display:flex;align-items:center;"><div class="led" id="plcMicroLogixLed"></div><span id="plcMicroLogixText">Connecting...</span></div></div>
                <div style="text-align:right;"><p id="lastUpdate" style="margin:0;font-size:0.9em;">Last updated: --</p></div>
            </div>
            <h2 style="color:#2980b9;"><div class="led" id="alarmPumpGpmLed" style="width:25px;height:25px;margin-right:15px;"></div><span id="currentGpm">Loading...</span></h2>
            <h2 id="currentLevel" style="color:#27ae60;font-size:1.8em;margin-top:15px;">Level: --</h2>
        </div>
        <div class="chart-container"><canvas id="flowChart"></canvas></div>
    </div>

    <!-- AQUIFER TAB -->
    <div id="aquifer-tab" class="tab-content">
        <div class="status-card">
            <div style="display:flex;justify-content:space-between;width:100%;max-width:600px;margin-bottom:15px;">
                <div style="text-align:left;"><div style="font-weight:bold;color:#7f8c8d;margin-bottom:5px;">Siemens S7-1200 PLC</div>
                <div style="display:flex;align-items:center;"><div class="led" id="plcSiemensLed"></div><span id="plcSiemensText">Connecting...</span></div></div>
                <div style="text-align:right;"><p id="aquiferLastUpdate" style="margin:0;font-size:0.9em;">Last updated: --</p></div>
            </div>
            <h2 id="currentAquiferLevel" style="color:#8e44ad;font-size:2.2em;margin-top:10px;">Loading...</h2>
        </div>
        <div class="chart-container"><canvas id="aquiferChart"></canvas></div>
    </div>

    <!-- DEVICES TAB -->
    <div id="devices-tab" class="tab-content">
        <div class="section-header">
            <h3>PLC / Modbus Devices</h3>
            <button class="btn-add" onclick="openDeviceModal()">+ Add Device</button>
        </div>
        <div style="overflow-x:auto;">
            <table class="data-table">
                <thead><tr>
                    <th style="text-align:center;width:80px;">Status</th>
                    <th>Name</th><th>Type</th><th>Host</th><th>Port</th>
                    <th>Description</th>
                    <th style="text-align:center;">Tags</th>
                    <th>Last Seen</th><th>Actions</th>
                </tr></thead>
                <tbody id="devicesBody">
                    <tr><td colspan="9" style="text-align:center;color:#aaa;padding:24px;">Loading...</td></tr>
                </tbody>
            </table>
        </div>

        <!-- Tags panel -->
        <div id="tagsSection" class="tag-section-wrap" style="display:none;">
            <div class="section-header">
                <h3>Tags &mdash; <span id="selectedDeviceName" style="color:#3498db;"></span></h3>
                <button class="btn-add" onclick="openTagModal()">+ Add Tag</button>
            </div>
            <div style="overflow-x:auto;">
                <table class="data-table">
                    <thead><tr>
                        <th>Name</th><th>Address</th><th>Data Type</th><th>Units</th>
                        <th>Last Value</th><th>Alarm</th>
                        <th style="text-align:center;">Enabled</th><th>Actions</th>
                    </tr></thead>
                    <tbody id="tagsBody">
                        <tr><td colspan="8" style="text-align:center;color:#aaa;padding:20px;">No tags yet.</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Device Modal -->
        <div id="deviceModal" class="modal-overlay" style="display:none;" onclick="if(event.target===this)closeDeviceModal()">
            <div class="modal-box">
                <h3 id="devModalTitle">Add Device</h3>
                <div class="form-row"><label>Name *</label><input type="text" id="devName" placeholder="e.g. Pump Room PLC"></div>
                <div class="form-row"><label>Type *</label>
                    <select id="devType" onchange="onDevTypeChange()">
                        <option value="allen_bradley_slc">Allen-Bradley SLC / MicroLogix</option>
                        <option value="allen_bradley_clx">Allen-Bradley ControlLogix / CompactLogix</option>
                        <option value="siemens_s7">Siemens S7</option>
                        <option value="modbus_tcp">Modbus TCP</option>
                    </select>
                </div>
                <div class="form-row"><label>Host / IP *</label><input type="text" id="devHost" placeholder="192.168.1.10 or hostname"></div>
                <div class="form-row"><label>Port</label><input type="number" id="devPort" min="1" max="65535"></div>
                <div id="devSiemensFields" style="display:none;">
                    <div class="form-row"><label>Rack</label><input type="number" id="devRack" value="0" min="0"></div>
                    <div class="form-row"><label>Slot</label><input type="number" id="devSlot" value="1" min="0"></div>
                </div>
                <div id="devModbusFields" style="display:none;">
                    <div class="form-row"><label>Unit ID (Slave ID)</label><input type="number" id="devUnitId" value="1" min="0" max="247"></div>
                    <div class="form-row form-inline">
                        <label>Word Swap</label>
                        <input type="checkbox" id="devWordSwap">
                        <small style="color:#7f8c8d;margin-left:6px;">Low word first (check if 32-bit values read incorrectly)</small>
                    </div>
                </div>
                <div class="form-row"><label>Description</label><input type="text" id="devDesc" placeholder="Optional notes"></div>
                <div class="form-row form-inline"><label>Enabled</label><input type="checkbox" id="devEnabled" checked></div>
                <div class="form-row" style="margin-top:10px;"><label>Password *</label><input type="password" id="devPassword" placeholder="Required to save"></div>
                <p id="devModalError" style="color:#e74c3c;font-size:0.88em;margin:6px 0 0;min-height:18px;"></p>
                <div style="display:flex;gap:10px;margin-top:18px;justify-content:flex-end;">
                    <button class="btn-cancel" onclick="closeDeviceModal()">Cancel</button>
                    <button class="btn-save" onclick="saveDevice()">Save</button>
                </div>
            </div>
        </div>

        <!-- Tag Modal -->
        <div id="tagModal" class="modal-overlay" style="display:none;" onclick="if(event.target===this)closeTagModal()">
            <div class="modal-box">
                <h3 id="tagModalTitle">Add Tag</h3>
                <div class="form-row"><label>Name *</label><input type="text" id="tagName" placeholder="e.g. Pump GPM"></div>
                <div class="form-row">
                    <label>Address *</label>
                    <input type="text" id="tagAddress" placeholder="">
                    <small id="tagAddrHint"></small>
                </div>
                <div class="form-row"><label>Data Type</label>
                    <select id="tagDataType">
                        <option value="REAL">REAL (Float)</option>
                        <option value="INT">INT</option>
                        <option value="DINT">DINT</option>
                        <option value="BOOL">BOOL</option>
                        <option value="WORD">WORD</option>
                    </select>
                </div>
                <div class="form-row"><label>Units</label><input type="text" id="tagUnits" placeholder="e.g. GPM, ft, psi, °F"></div>
                <div class="form-row"><label>Description</label><input type="text" id="tagDesc" placeholder="Optional notes"></div>
                <div class="form-grid2">
                    <div class="form-row"><label>Scale Factor</label><input type="number" id="tagScaleFactor" step="any" value="1" placeholder="1"><small>Multiply raw value by this (default: 1)</small></div>
                    <div class="form-row"><label>Scale Offset</label><input type="number" id="tagScaleOffset" step="any" value="0" placeholder="0"><small>Added after scaling (default: 0)</small></div>
                </div>
                <div style="font-size:0.8em;color:#7f8c8d;margin-bottom:12px;padding:6px 10px;background:#f8f9fa;border-radius:4px;">Result&nbsp;=&nbsp;(raw&nbsp;&times;&nbsp;factor)&nbsp;+&nbsp;offset&nbsp;&nbsp;&middot;&nbsp;&nbsp;e.g.&nbsp;raw&nbsp;217&nbsp;&times;&nbsp;0.01&nbsp;=&nbsp;2.17</div>
                <div class="form-row form-inline"><label>Enabled</label><input type="checkbox" id="tagEnabled" checked></div>

                <!-- ── Alarm Section ── -->
                <hr class="alarm-divider">
                <div class="alarm-section-title">🔔 Alarm</div>
                <div class="form-row form-inline">
                    <label>Enable Alarm</label>
                    <input type="checkbox" id="tagAlarmEnabled" onchange="onAlarmToggle()">
                </div>
                <div id="alarmFields" style="display:none;">
                    <div class="form-grid2">
                        <div class="form-row">
                            <label>Low Alarm — alert when below</label>
                            <input type="number" id="tagAlarmLow" step="any" placeholder="No low alarm">
                        </div>
                        <div class="form-row">
                            <label>High Alarm — alert when above</label>
                            <input type="number" id="tagAlarmHigh" step="any" placeholder="No high alarm">
                        </div>
                    </div>
                    <div class="form-row">
                        <label>Custom Message <span style="color:#aaa;">(optional)</span></label>
                        <input type="text" id="tagAlarmMessage" placeholder="Leave blank for auto-generated message">
                    </div>
                </div>

                <div class="form-row" style="margin-top:10px;"><label>Password *</label><input type="password" id="tagPassword" placeholder="Required to save"></div>
                <p id="tagModalError" style="color:#e74c3c;font-size:0.88em;margin:6px 0 0;min-height:18px;"></p>
                <div style="display:flex;gap:10px;margin-top:18px;justify-content:flex-end;">
                    <button class="btn-cancel" onclick="closeTagModal()">Cancel</button>
                    <button class="btn-save" onclick="saveTag()">Save</button>
                </div>
            </div>
        </div>

    </div><!-- /devices-tab -->

    <!-- Global time controls -->
    <div class="controls" id="timeControls" style="margin-top:20px;">
        <button id="btn1440" onclick="setTimeWindow(1440)">60 Days</button>
        <button id="btn168"  onclick="setTimeWindow(168)">7 Days</button>
        <button id="btn24"   onclick="setTimeWindow(24)">24 Hrs</button>
        <button id="btn6"    onclick="setTimeWindow(6)">6 Hrs</button>
        <button id="btn1"    onclick="setTimeWindow(1)" class="active">Reset (1 Hr)</button>
        <div class="custom-range">
            <input type="datetime-local" id="startTime">
            <span>to</span>
            <input type="datetime-local" id="endTime">
            <button onclick="setCustomRange()">Apply Custom Range</button>
        </div>
    </div>
</div>

<script>
// ═══════════════════════════════════════════ CHART / DATA
let flowChart, aquiferChart;
let currentHours=1, customStart=null, customEnd=null, activeTab='pump';

function switchTab(tabName) {
    activeTab = tabName;
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b=>{ if(b.dataset.tab===tabName) b.classList.add('active'); });
    document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
    document.getElementById(tabName+'-tab').classList.add('active');
    const tc = document.getElementById('timeControls');
    if (tabName==='devices') {
        tc.style.display='none';
        fetchDevices();
        if (!devicesTimer) devicesTimer=setInterval(fetchDeviceStatus,60000);
    } else {
        if (devicesTimer){clearInterval(devicesTimer);devicesTimer=null;}
        tc.style.display='block';
        fetchData();
    }
}

function setTimeWindow(hours) {
    currentHours=hours; customStart=null; customEnd=null;
    document.querySelectorAll('.controls button').forEach(b=>b.classList.remove('active'));
    document.getElementById('btn'+hours).classList.add('active');
    if (activeTab==='pump'&&flowChart) flowChart.resetZoom();
    if (activeTab==='aquifer'&&aquiferChart) aquiferChart.resetZoom();
    fetchData();
}
function setCustomRange() {
    customStart=document.getElementById('startTime').value;
    customEnd=document.getElementById('endTime').value;
    if (!customStart||!customEnd){alert('Please select both a start and end time.');return;}
    document.querySelectorAll('.controls button').forEach(b=>b.classList.remove('active'));
    currentHours=0;
    if (activeTab==='pump'&&flowChart) flowChart.resetZoom();
    if (activeTab==='aquifer'&&aquiferChart) aquiferChart.resetZoom();
    fetchData();
}

async function fetchData() {
    try {
        let url='/api/data?tab='+activeTab+'&hours='+currentHours;
        if (currentHours===0&&customStart&&customEnd) url='/api/data?tab='+activeTab+'&start='+customStart+'&end='+customEnd;
        const data=await(await fetch(url)).json();
        if (!data.length) return;
        const latest=data[data.length-1];
        const localTime=moment.utc(latest.x).local().format('YYYY-MM-DD HH:mm:ss');
        const isConn=moment().diff(moment.utc(latest.x).local(),'minutes')<3;
        if (activeTab==='pump') {
            document.getElementById('plcMicroLogixLed').className='led '+(isConn?'green':'red');
            document.getElementById('plcMicroLogixText').innerText=isConn?'Connected':'Offline';
            document.getElementById('alarmPumpGpmLed').className='led '+(latest.y>=2000?'green':'red');
            document.getElementById('currentGpm').innerText=latest.y+' GPM';
            document.getElementById('currentLevel').innerText='Level: '+(latest.level!==null?latest.level:'--');
            document.getElementById('lastUpdate').innerText='Last updated: '+localTime;
            const gpmData=data.map(d=>({x:d.x,y:d.y})), levelData=data.map(d=>({x:d.x,y:d.level}));
            if (!flowChart) initChart(gpmData,levelData);
            else{flowChart.data.datasets[0].data=gpmData;flowChart.data.datasets[1].data=levelData;flowChart.update();}
        } else {
            document.getElementById('plcSiemensLed').className='led '+(isConn?'green':'red');
            document.getElementById('plcSiemensText').innerText=isConn?'Connected':'Offline';
            document.getElementById('currentAquiferLevel').innerText=latest.y+' ft';
            document.getElementById('aquiferLastUpdate').innerText='Last updated: '+localTime;
            const aqData=data.map(d=>({x:d.x,y:d.y}));
            if (!aquiferChart) initAquiferChart(aqData);
            else{aquiferChart.data.datasets[0].data=aqData;aquiferChart.update();}
        }
    } catch(e){console.error('fetchData error',e);}
}

function initChart(gpmData,levelData) {
    flowChart=new Chart(document.getElementById('flowChart').getContext('2d'),{type:'line',data:{datasets:[
        {label:'Pump Flow (GPM)',data:gpmData,borderColor:'#3498db',backgroundColor:'rgba(52,152,219,0.1)',borderWidth:2,pointRadius:0,pointHoverRadius:4,fill:true,tension:0.1,yAxisID:'y'},
        {label:'Water Level',data:levelData,borderColor:'#27ae60',backgroundColor:'rgba(39,174,96,0.1)',borderWidth:2,pointRadius:0,pointHoverRadius:4,fill:true,tension:0.1,yAxisID:'y1'}
    ]},options:{responsive:true,maintainAspectRatio:false,scales:{
        x:{type:'time',time:{unit:'minute',displayFormats:{minute:'HH:mm'}},title:{display:true,text:'Time'}},
        y:{type:'linear',display:true,position:'left',title:{display:true,text:'GPM'},suggestedMin:0,suggestedMax:6000},
        y1:{type:'linear',display:true,position:'right',title:{display:true,text:'Level'},suggestedMin:0,grid:{drawOnChartArea:false}}
    },plugins:{legend:{display:true},tooltip:{mode:'index',intersect:false},
        zoom:{limits:{x:{min:'original',max:'original'}},pan:{enabled:true,mode:'x'},zoom:{wheel:{enabled:true},pinch:{enabled:true},mode:'x'}}
    },interaction:{mode:'nearest',axis:'x',intersect:false}}});
}
function initAquiferChart(aqData) {
    aquiferChart=new Chart(document.getElementById('aquiferChart').getContext('2d'),{type:'line',data:{datasets:[
        {label:'Aquifer Level (ft)',data:aqData,borderColor:'#8e44ad',backgroundColor:'rgba(142,68,173,0.1)',borderWidth:2,pointRadius:0,pointHoverRadius:4,fill:true,tension:0.1}
    ]},options:{responsive:true,maintainAspectRatio:false,scales:{
        x:{type:'time',time:{unit:'minute',displayFormats:{minute:'HH:mm'}},title:{display:true,text:'Time'}},
        y:{type:'linear',display:true,position:'left',title:{display:true,text:'Level'}}
    },plugins:{legend:{display:true},tooltip:{mode:'index',intersect:false},
        zoom:{limits:{x:{min:'original',max:'original'}},pan:{enabled:true,mode:'x'},zoom:{wheel:{enabled:true},pinch:{enabled:true},mode:'x'}}
    },interaction:{mode:'nearest',axis:'x',intersect:false}}});
}

async function loadThreshold() {
    try{const d=await(await fetch('/api/threshold')).json();document.getElementById('thresholdInput').value=d.threshold_gpm;}catch(e){}
}
async function saveThreshold() {
    const val=parseInt(document.getElementById('thresholdInput').value);
    const password=document.getElementById('thresholdPassword').value;
    const st=document.getElementById('thresholdStatus');
    if(isNaN(val)||val<0){st.style.color='#e74c3c';st.innerText='Invalid value';return;}
    if(!password){st.style.color='#e74c3c';st.innerText='Password required';return;}
    try{
        const res=await fetch('/api/threshold',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({threshold_gpm:val,password})});
        const d=await res.json();
        if(d.threshold_gpm!==undefined){st.style.color='#27ae60';st.innerText='✓ Saved';document.getElementById('thresholdPassword').value='';setTimeout(()=>st.innerText='',3000);}
        else if(d.error==='Unauthorized'){st.style.color='#e74c3c';st.innerText='Incorrect password';}
        else{st.style.color='#e74c3c';st.innerText='Error saving';}
    }catch(e){st.style.color='#e74c3c';st.innerText='Error saving';}
}

// ═══════════════════════════════════════════ DEVICES TAB
let devices=[],deviceStatus={},devicesTimer=null;
let selectedDeviceId=null,editingDeviceId=null,editingTagId=null;

const TYPE_LABELS={allen_bradley_slc:'AB SLC/MicroLogix',allen_bradley_clx:'AB ControlLogix',siemens_s7:'Siemens S7',modbus_tcp:'Modbus TCP'};
const TYPE_BADGE={allen_bradley_slc:'badge-ab-slc',allen_bradley_clx:'badge-ab-clx',siemens_s7:'badge-siemens',modbus_tcp:'badge-modbus'};
const TYPE_DEFAULT_PORTS={allen_bradley_slc:44818,allen_bradley_clx:44818,siemens_s7:102,modbus_tcp:502};
const TYPE_ADDR_HINTS={allen_bradley_slc:'e.g. N7:6  F8:2  B3:0',allen_bradley_clx:'e.g. MyTag  Array[0]',siemens_s7:'e.g. DB6.REAL108  DB1.DBX0.0',modbus_tcp:'e.g. HR4800  IR200  CO5  (1-indexed)'};

function escHtml(s){if(s==null)return '';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

function ledDot(on,size){
    size=size||14;
    const c=on?'#2ecc71':'#e74c3c';
    return '<span title="'+(on?'Connected':'Offline/No data')+'" style="display:inline-block;width:'+size+'px;height:'+size+'px;border-radius:50%;background:'+c+';box-shadow:0 0 5px '+c+';vertical-align:middle;"></span>';
}

async function fetchDeviceStatus() {
    try{
        const [devRes,statRes]=await Promise.all([fetch('/api/devices'),fetch('/api/device_status')]);
        devices=await devRes.json(); deviceStatus=await statRes.json();
        renderDevices();
        if(selectedDeviceId){const dev=devices.find(d=>d.id===selectedDeviceId);if(dev)renderTags(dev.tags,dev.name,dev.type);}
    }catch(e){}
}

async function fetchDevices() {
    try{
        const[devRes,statRes]=await Promise.all([fetch('/api/devices'),fetch('/api/device_status')]);
        devices=await devRes.json(); deviceStatus=await statRes.json();
        renderDevices();
        if(selectedDeviceId){
            const dev=devices.find(d=>d.id===selectedDeviceId);
            if(dev)renderTags(dev.tags,dev.name,dev.type);
            else document.getElementById('tagsSection').style.display='none';
        }
    }catch(e){
        document.getElementById('devicesBody').innerHTML='<tr><td colspan="9" style="color:#e74c3c;text-align:center;padding:20px;">Error loading devices</td></tr>';
    }
}

function renderDevices(){
    const tbody=document.getElementById('devicesBody');
    if(!devices.length){tbody.innerHTML='<tr><td colspan="9" style="text-align:center;color:#aaa;padding:24px;">No devices configured. Click <strong>+ Add Device</strong>.</td></tr>';return;}
    let html='';
    devices.forEach(d=>{
        const badge='<span class="badge '+(TYPE_BADGE[d.type]||'badge-unknown')+'">'+escHtml(TYPE_LABELS[d.type]||d.type)+'</span>';
        const isSel=d.id===selectedDeviceId;
        const tagCount=d.tags?d.tags.length:0;
        const stat=deviceStatus[String(d.id)]||{};
        const isConn=stat.connected||false;
        const led=ledDot(isConn);
        const connTxt=isConn?'Connected':(stat.last_seen?'Offline':'No data');
        const connClr=isConn?'#27ae60':(stat.last_seen?'#c0392b':'#95a5a6');
        const lastSeen=stat.last_seen?moment.utc(stat.last_seen).local().format('MM/DD HH:mm:ss'):'–';
        html+='<tr class="'+(isSel?'selected':'')+'" style="cursor:pointer;" onclick="selectDevice('+d.id+')">'
            +'<td style="text-align:center;">'+led+'<br><span style="font-size:0.68em;color:'+connClr+';font-weight:600;">'+connTxt+'</span></td>'
            +'<td><strong>'+escHtml(d.name)+'</strong></td>'
            +'<td>'+badge+'</td>'
            +'<td style="font-family:monospace;font-size:0.88em;">'+escHtml(d.host)+'</td>'
            +'<td>'+(d.port||'–')+'</td>'
            +'<td style="color:#7f8c8d;font-size:0.88em;">'+escHtml(d.description)+'</td>'
            +'<td style="text-align:center;"><span style="background:#eaf2ff;color:#2980b9;padding:2px 8px;border-radius:10px;font-size:0.85em;font-weight:600;">'+tagCount+'</span></td>'
            +'<td style="font-size:0.84em;color:#555;white-space:nowrap;">'+lastSeen+'</td>'
            +'<td>'
            +'<button class="btn-edit" onclick="event.stopPropagation();openDeviceModal('+d.id+')">Edit</button>'
            +'<button class="btn-del" onclick="event.stopPropagation();deleteDevice('+d.id+')">Del</button>'
            +'</td></tr>';
    });
    tbody.innerHTML=html;
}

function selectDevice(id){
    selectedDeviceId=id;
    const dev=devices.find(d=>d.id===id);
    renderDevices();
    if(dev)renderTags(dev.tags,dev.name,dev.type);
}

function renderTags(tags,deviceName,deviceType){
    document.getElementById('tagsSection').style.display='block';
    document.getElementById('selectedDeviceName').innerText=deviceName;
    const tbody=document.getElementById('tagsBody');
    const tStat=(deviceStatus[String(selectedDeviceId)]||{}).tags||{};
    if(!tags||!tags.length){tbody.innerHTML='<tr><td colspan="8" style="text-align:center;color:#aaa;padding:18px;">No tags yet. Click <strong>+ Add Tag</strong>.</td></tr>';return;}
    let html='';
    tags.forEach(t=>{
        const ts=tStat[String(t.id)]||{};
        const hasVal=ts.value!=null;
        const led=ledDot(hasVal,11);
        let lastVal='<span style="color:#bdc3c7;">–</span>';
        if(hasVal){lastVal='<strong style="color:#2c3e50;">'+ts.value+'</strong>';if(t.units)lastVal+=' <span style="color:#7f8c8d;font-size:0.84em;">'+escHtml(t.units)+'</span>';}
        const sf=t.scale_factor!=null?t.scale_factor:1;
        const so=t.scale_offset!=null?t.scale_offset:0;
        if(sf!==1||so!==0){lastVal+=' <span title="Scale: \xd7'+sf+(so!==0?(so>0?\'+\':\'\')+ so:\'\')+'\" style=\"font-size:0.75em;color:#8e44ad;background:#f5eeff;padding:1px 5px;border-radius:8px;margin-left:3px;\">\xd7'+sf+'</span>';}

        // Alarm column
        let alarmHtml='<span style="color:#bdc3c7;font-size:0.85em;">–</span>';
        if(t.alarm_enabled){
            const isActive=ts.alarm_active;
            let thresholdStr='';
            if(t.alarm_low!=null) thresholdStr+='L:'+t.alarm_low+' ';
            if(t.alarm_high!=null) thresholdStr+='H:'+t.alarm_high;
            thresholdStr=(thresholdStr.trim()||'set')+' '+(t.units?escHtml(t.units):'');
            if(isActive){
                alarmHtml='<span class="alarm-badge alarm-active">🚨 ALARM</span><br><small>'+thresholdStr.trim()+'</small>';
            } else {
                alarmHtml='<span class="alarm-badge alarm-on">🔔 '+thresholdStr.trim()+'</span>';
            }
        }

        html+='<tr>'
            +'<td>'+led+' <strong>'+escHtml(t.name)+'</strong></td>'
            +'<td><span class="tag-code">'+escHtml(t.address)+'</span></td>'
            +'<td>'+escHtml(t.data_type)+'</td>'
            +'<td>'+(escHtml(t.units)||'–')+'</td>'
            +'<td>'+lastVal+'</td>'
            +'<td>'+alarmHtml+'</td>'
            +'<td style="text-align:center;">'+(t.enabled?'✅':'❌')+'</td>'
            +'<td>'
            +'<button class="btn-edit" onclick="openTagModal('+t.id+')">Edit</button>'
            +'<button class="btn-del" onclick="deleteTag('+t.id+')">Del</button>'
            +'</td></tr>';
    });
    tbody.innerHTML=html;
}

// ── Device Modal ──────────────────────────────────────────────────────────
function openDeviceModal(deviceId){
    editingDeviceId=deviceId||null;
    document.getElementById('devModalError').innerText='';
    document.getElementById('devPassword').value='';
    if(deviceId){
        const dev=devices.find(d=>d.id===deviceId); if(!dev)return;
        document.getElementById('devModalTitle').innerText='Edit Device';
        document.getElementById('devName').value=dev.name;
        document.getElementById('devType').value=dev.type;
        document.getElementById('devHost').value=dev.host;
        document.getElementById('devPort').value=dev.port||'';
        document.getElementById('devRack').value=dev.rack!=null?dev.rack:0;
        document.getElementById('devSlot').value=dev.slot!=null?dev.slot:1;
        document.getElementById('devUnitId').value=dev.unit_id!=null?dev.unit_id:1;
        document.getElementById('devWordSwap').checked=!!dev.word_swap;
        document.getElementById('devDesc').value=dev.description||'';
        document.getElementById('devEnabled').checked=!!dev.enabled;
    }else{
        document.getElementById('devModalTitle').innerText='Add Device';
        document.getElementById('devName').value='';
        document.getElementById('devType').value='allen_bradley_slc';
        document.getElementById('devHost').value='';
        document.getElementById('devPort').value=44818;
        document.getElementById('devRack').value=0;
        document.getElementById('devSlot').value=1;
        document.getElementById('devUnitId').value=1;
        document.getElementById('devWordSwap').checked=false;
        document.getElementById('devDesc').value='';
        document.getElementById('devEnabled').checked=true;
    }
    onDevTypeChange();
    document.getElementById('deviceModal').style.display='flex';
}
function closeDeviceModal(){document.getElementById('deviceModal').style.display='none';editingDeviceId=null;}
function onDevTypeChange(){
    const type=document.getElementById('devType').value;
    document.getElementById('devSiemensFields').style.display=type==='siemens_s7'?'block':'none';
    document.getElementById('devModbusFields').style.display=type==='modbus_tcp'?'block':'none';
    const portEl=document.getElementById('devPort');
    const cur=parseInt(portEl.value)||0;
    if(!cur||Object.values(TYPE_DEFAULT_PORTS).includes(cur))portEl.value=TYPE_DEFAULT_PORTS[type]||'';
}
async function saveDevice(){
    const errEl=document.getElementById('devModalError');errEl.innerText='';
    const name=document.getElementById('devName').value.trim();
    const type=document.getElementById('devType').value;
    const host=document.getElementById('devHost').value.trim();
    const port=parseInt(document.getElementById('devPort').value)||0;
    const rack=parseInt(document.getElementById('devRack').value)||0;
    const slot=parseInt(document.getElementById('devSlot').value)||1;
    const unit_id=parseInt(document.getElementById('devUnitId').value)||1;
    const word_swap=document.getElementById('devWordSwap').checked?1:0;
    const description=document.getElementById('devDesc').value.trim();
    const enabled=document.getElementById('devEnabled').checked;
    const password=document.getElementById('devPassword').value;
    if(!name){errEl.innerText='Name is required.';return;}
    if(!host){errEl.innerText='Host is required.';return;}
    if(!password){errEl.innerText='Password is required.';return;}
    try{
        const url=editingDeviceId?'/api/devices/'+editingDeviceId:'/api/devices';
        const method=editingDeviceId?'PUT':'POST';
        const res=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify({name,type,host,port,rack,slot,unit_id,word_swap,description,enabled,password})});
        const d=await res.json();
        if(!res.ok){errEl.innerText=d.error==='Unauthorized'?'Incorrect password.':(d.error||'Error saving.');return;}
        closeDeviceModal();fetchDevices();
    }catch(e){errEl.innerText='Network error.';}
}
async function deleteDevice(id){
    const dev=devices.find(d=>d.id===id);if(!dev)return;
    const pwd=prompt('Enter password to delete "'+dev.name+'" and ALL its tags:');
    if(pwd===null)return;if(!pwd){alert('Password required.');return;}
    if(!confirm('Delete "'+dev.name+'" and all its tags? This cannot be undone.'))return;
    try{
        const res=await fetch('/api/devices/'+id,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pwd})});
        const d=await res.json();
        if(!res.ok){alert(d.error==='Unauthorized'?'Incorrect password.':(d.error||'Error deleting.'));return;}
        if(selectedDeviceId===id){selectedDeviceId=null;document.getElementById('tagsSection').style.display='none';}
        fetchDevices();
    }catch(e){alert('Network error.');}
}

// ── Tag Modal ─────────────────────────────────────────────────────────────
function onAlarmToggle(){
    document.getElementById('alarmFields').style.display=document.getElementById('tagAlarmEnabled').checked?'block':'none';
}
function openTagModal(tagId){
    editingTagId=tagId||null;
    const dev=devices.find(d=>d.id===selectedDeviceId);
    const devType=dev?dev.type:'allen_bradley_slc';
    document.getElementById('tagModalError').innerText='';
    document.getElementById('tagPassword').value='';
    document.getElementById('tagAddrHint').innerText=TYPE_ADDR_HINTS[devType]||'';
    if(tagId&&dev&&dev.tags){
        const tag=dev.tags.find(t=>t.id===tagId);if(!tag)return;
        document.getElementById('tagModalTitle').innerText='Edit Tag';
        document.getElementById('tagName').value=tag.name;
        document.getElementById('tagAddress').value=tag.address;
        document.getElementById('tagDataType').value=tag.data_type;
        document.getElementById('tagUnits').value=tag.units||'';
        document.getElementById('tagDesc').value=tag.description||'';
        document.getElementById('tagEnabled').checked=!!tag.enabled;
        document.getElementById('tagAlarmEnabled').checked=!!tag.alarm_enabled;
        document.getElementById('tagAlarmLow').value=tag.alarm_low!=null?tag.alarm_low:'';
        document.getElementById('tagAlarmHigh').value=tag.alarm_high!=null?tag.alarm_high:'';
        document.getElementById('tagAlarmMessage').value=tag.alarm_message||'';
        document.getElementById('tagScaleFactor').value=tag.scale_factor!=null?tag.scale_factor:1;
        document.getElementById('tagScaleOffset').value=tag.scale_offset!=null?tag.scale_offset:0;
    }else{
        document.getElementById('tagModalTitle').innerText='Add Tag';
        document.getElementById('tagName').value='';
        document.getElementById('tagAddress').value='';
        document.getElementById('tagDataType').value='REAL';
        document.getElementById('tagUnits').value='';
        document.getElementById('tagDesc').value='';
        document.getElementById('tagEnabled').checked=true;
        document.getElementById('tagAlarmEnabled').checked=false;
        document.getElementById('tagAlarmLow').value='';
        document.getElementById('tagAlarmHigh').value='';
        document.getElementById('tagAlarmMessage').value='';
        document.getElementById('tagScaleFactor').value=1;
        document.getElementById('tagScaleOffset').value=0;
    }
    onAlarmToggle();
    document.getElementById('tagModal').style.display='flex';
}
function closeTagModal(){document.getElementById('tagModal').style.display='none';editingTagId=null;}
async function saveTag(){
    const errEl=document.getElementById('tagModalError');errEl.innerText='';
    const name=document.getElementById('tagName').value.trim();
    const address=document.getElementById('tagAddress').value.trim();
    const data_type=document.getElementById('tagDataType').value;
    const units=document.getElementById('tagUnits').value.trim();
    const description=document.getElementById('tagDesc').value.trim();
    const enabled=document.getElementById('tagEnabled').checked;
    const alarm_enabled=document.getElementById('tagAlarmEnabled').checked?1:0;
    const alarm_low_raw=document.getElementById('tagAlarmLow').value;
    const alarm_high_raw=document.getElementById('tagAlarmHigh').value;
    const alarm_low=alarm_low_raw!==''?parseFloat(alarm_low_raw):null;
    const alarm_high=alarm_high_raw!==''?parseFloat(alarm_high_raw):null;
    const alarm_message=document.getElementById('tagAlarmMessage').value.trim();
    const scale_factor_raw=document.getElementById('tagScaleFactor').value;
    const scale_offset_raw=document.getElementById('tagScaleOffset').value;
    const scale_factor=scale_factor_raw!==''?parseFloat(scale_factor_raw):1.0;
    const scale_offset=scale_offset_raw!==''?parseFloat(scale_offset_raw):0.0;
    const password=document.getElementById('tagPassword').value;
    if(!name){errEl.innerText='Tag name is required.';return;}
    if(!address){errEl.innerText='Address is required.';return;}
    if(!password){errEl.innerText='Password is required.';return;}
    try{
        const url=editingTagId?'/api/tags/'+editingTagId:'/api/tags';
        const method=editingTagId?'PUT':'POST';
        const res=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify({name,address,data_type,units,description,enabled,alarm_enabled,alarm_low,alarm_high,alarm_message,scale_factor,scale_offset,password,device_id:selectedDeviceId})});
        const d=await res.json();
        if(!res.ok){errEl.innerText=d.error==='Unauthorized'?'Incorrect password.':(d.error||'Error saving.');return;}
        closeTagModal();fetchDevices();
    }catch(e){errEl.innerText='Network error.';}
}
async function deleteTag(id){
    const pwd=prompt('Enter password to delete this tag:');
    if(pwd===null)return;if(!pwd){alert('Password required.');return;}
    if(!confirm('Delete this tag? This cannot be undone.'))return;
    try{
        const res=await fetch('/api/tags/'+id,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pwd})});
        const d=await res.json();
        if(!res.ok){alert(d.error==='Unauthorized'?'Incorrect password.':(d.error||'Error deleting.'));return;}
        fetchDevices();
    }catch(e){alert('Network error.');}
}

loadThreshold();fetchData();setInterval(fetchData,60000);
</script>
</body>
</html>
"""


# ════════════════════════════════════════════════════════════ FLASK ROUTES

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/threshold', methods=['GET','POST'])
def threshold():
    if request.method=='POST':
        data=request.get_json()
        if not data or data.get('password')!=THRESHOLD_PASSWORD:
            return jsonify({'error':'Unauthorized'}),401
        val=data.get('threshold_gpm')
        if val is None or not str(val).isdigit():
            return jsonify({'error':'Invalid value'}),400
        if set_threshold(val):
            return jsonify({'threshold_gpm':int(val)})
        return jsonify({'error':'Failed to save'}),500
    return jsonify({'threshold_gpm':get_threshold()})


@app.route('/api/data')
def get_data():
    tab=request.args.get('tab',default='pump')
    hours=request.args.get('hours',default=1,type=int)
    start=request.args.get('start'); end=request.args.get('end')
    try:
        conn=sqlite3.connect(DB_PATH); conn.row_factory=sqlite3.Row; cursor=conn.cursor()
        table="flow_log" if tab=='pump' else "aquifer_log"
        qcols="timestamp,gpm,level" if tab=='pump' else "timestamp,level as y"
        if start and end:
            tz=zoneinfo.ZoneInfo("America/Chicago")
            try:
                s_dt=datetime.datetime.strptime(start,'%Y-%m-%dT%H:%M').replace(tzinfo=tz)
                e_dt=datetime.datetime.strptime(end,'%Y-%m-%dT%H:%M').replace(tzinfo=tz)
                s_utc=s_dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                e_utc=e_dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                cursor.execute(f"SELECT {qcols} FROM {table} WHERE timestamp>=? AND timestamp<=? ORDER BY timestamp ASC",(s_utc,e_utc))
            except ValueError:
                cursor.execute(f"SELECT {qcols} FROM {table} WHERE timestamp>=datetime('now','-1 hours') ORDER BY timestamp ASC")
        else:
            if hours not in [1,6,24,168,1440]: hours=1
            cursor.execute(f"SELECT {qcols} FROM {table} WHERE timestamp>=datetime('now','-{hours} hours') ORDER BY timestamp ASC")
        rows=cursor.fetchall()
        data=[{"x":r["timestamp"]+"Z","y":r["gpm"],"level":r["level"]} for r in rows] if tab=='pump' else [{"x":r["timestamp"]+"Z","y":r["y"]} for r in rows]
        conn.close(); return jsonify(data)
    except Exception as e:
        return jsonify({"error":str(e)}),500


@app.route('/api/device_status')
def device_status():
    try:
        conn=sqlite3.connect(DB_PATH); conn.row_factory=sqlite3.Row
        devs=conn.execute("SELECT * FROM devices ORDER BY id").fetchall()
        result={}
        for dev in devs:
            dev_id=dev['id']
            tags=conn.execute("SELECT id,name,units,address,log_source,alarm_enabled,alarm_low,alarm_high FROM tags WHERE device_id=? ORDER BY id",(dev_id,)).fetchall()
            tag_status={}; last_timestamps=[]
            for tag in tags:
                value=None; timestamp=None
                if tag['log_source']:
                    try:
                        src=json.loads(tag['log_source']); tbl=src['table']; col=src['column']
                        if tbl in ('flow_log','aquifer_log') and col.replace('_','').isalpha():
                            row=conn.execute(f"SELECT {col},timestamp FROM {tbl} ORDER BY timestamp DESC LIMIT 1").fetchone()
                            if row: value=row[col]; timestamp=row['timestamp']
                    except Exception: pass
                if value is None:
                    row=conn.execute("SELECT value,timestamp FROM tag_readings WHERE tag_id=? ORDER BY timestamp DESC LIMIT 1",(tag['id'],)).fetchone()
                    if row: value=row['value']; timestamp=row['timestamp']
                if timestamp: last_timestamps.append(timestamp)
                # Alarm state from DB
                arow=conn.execute("SELECT active FROM tag_alarm_state WHERE tag_id=?",(tag['id'],)).fetchone()
                alarm_active=bool(arow['active']) if arow else False
                tag_status[str(tag['id'])]={
                    'name':tag['name'],'units':tag['units'] or '',
                    'value':value,'timestamp':timestamp,'alarm_active':alarm_active
                }
            connected=False; last_seen=None
            if last_timestamps:
                last_seen=max(last_timestamps)
                try:
                    ts=datetime.datetime.strptime(last_seen,'%Y-%m-%d %H:%M:%S')
                    connected=(datetime.datetime.utcnow()-ts).total_seconds()<180
                except Exception: pass
            result[str(dev_id)]={'connected':connected,'last_seen':last_seen,'tags':tag_status}
        conn.close(); return jsonify(result)
    except Exception as e:
        return jsonify({'error':str(e)}),500


@app.route('/api/devices',methods=['GET'])
def list_devices():
    try:
        conn=sqlite3.connect(DB_PATH); conn.row_factory=sqlite3.Row
        devs=conn.execute("SELECT * FROM devices ORDER BY id").fetchall()
        result=[]
        for d in devs:
            dd=dict(d)
            tags=conn.execute("SELECT * FROM tags WHERE device_id=? ORDER BY id",(d['id'],)).fetchall()
            dd['tags']=[dict(t) for t in tags]; result.append(dd)
        conn.close(); return jsonify(result)
    except Exception as e:
        return jsonify({'error':str(e)}),500


@app.route('/api/devices',methods=['POST'])
def create_device():
    data=request.get_json()
    if not data or data.get('password')!=THRESHOLD_PASSWORD:
        return jsonify({'error':'Unauthorized'}),401
    if not data.get('name') or not data.get('type') or not data.get('host'):
        return jsonify({'error':'name, type, and host are required'}),400
    try:
        conn=sqlite3.connect(DB_PATH,isolation_level=None)
        conn.execute("INSERT INTO devices (name,type,host,port,rack,slot,unit_id,word_swap,description,enabled) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (data['name'],data['type'],data.get('host',''),data.get('port',0),data.get('rack',0),data.get('slot',1),
             data.get('unit_id',1),data.get('word_swap',0),data.get('description',''),1 if data.get('enabled',True) else 0))
        new_id=conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.row_factory=sqlite3.Row
        dev=dict(conn.execute("SELECT * FROM devices WHERE id=?",(new_id,)).fetchone()); dev['tags']=[]
        conn.close(); return jsonify(dev),201
    except Exception as e:
        return jsonify({'error':str(e)}),500


@app.route('/api/devices/<int:device_id>',methods=['PUT'])
def update_device(device_id):
    data=request.get_json()
    if not data or data.get('password')!=THRESHOLD_PASSWORD:
        return jsonify({'error':'Unauthorized'}),401
    try:
        conn=sqlite3.connect(DB_PATH,isolation_level=None)
        conn.execute("UPDATE devices SET name=?,type=?,host=?,port=?,rack=?,slot=?,unit_id=?,word_swap=?,description=?,enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (data['name'],data['type'],data.get('host',''),data.get('port',0),data.get('rack',0),data.get('slot',1),
             data.get('unit_id',1),data.get('word_swap',0),data.get('description',''),1 if data.get('enabled',True) else 0,device_id))
        conn.row_factory=sqlite3.Row
        row=conn.execute("SELECT * FROM devices WHERE id=?",(device_id,)).fetchone()
        conn.close()
        if not row: return jsonify({'error':'Not found'}),404
        return jsonify(dict(row))
    except Exception as e:
        return jsonify({'error':str(e)}),500


@app.route('/api/devices/<int:device_id>',methods=['DELETE'])
def delete_device(device_id):
    data=request.get_json()
    if not data or data.get('password')!=THRESHOLD_PASSWORD:
        return jsonify({'error':'Unauthorized'}),401
    try:
        conn=sqlite3.connect(DB_PATH,isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM devices WHERE id=?",(device_id,))
        conn.close(); return jsonify({'ok':True})
    except Exception as e:
        return jsonify({'error':str(e)}),500


@app.route('/api/tags',methods=['GET'])
def list_tags():
    device_id=request.args.get('device_id',type=int)
    try:
        conn=sqlite3.connect(DB_PATH); conn.row_factory=sqlite3.Row
        rows=conn.execute("SELECT * FROM tags WHERE device_id=? ORDER BY id",(device_id,)).fetchall() if device_id else conn.execute("SELECT * FROM tags ORDER BY device_id,id").fetchall()
        conn.close(); return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error':str(e)}),500


@app.route('/api/tags',methods=['POST'])
def create_tag():
    data=request.get_json()
    if not data or data.get('password')!=THRESHOLD_PASSWORD:
        return jsonify({'error':'Unauthorized'}),401
    if not data.get('device_id') or not data.get('name') or not data.get('address'):
        return jsonify({'error':'device_id, name, and address are required'}),400
    try:
        conn=sqlite3.connect(DB_PATH,isolation_level=None)
        conn.execute("INSERT INTO tags (device_id,name,address,data_type,units,description,alarm_enabled,alarm_low,alarm_high,alarm_message,scale_factor,scale_offset,enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (data['device_id'],data['name'],data['address'],data.get('data_type','REAL'),data.get('units',''),
             data.get('description',''),data.get('alarm_enabled',0),data.get('alarm_low'),data.get('alarm_high'),
             data.get('alarm_message',''),data.get('scale_factor',1.0),data.get('scale_offset',0.0),
             1 if data.get('enabled',True) else 0))
        new_id=conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.row_factory=sqlite3.Row
        tag=dict(conn.execute("SELECT * FROM tags WHERE id=?",(new_id,)).fetchone())
        conn.close(); return jsonify(tag),201
    except Exception as e:
        return jsonify({'error':str(e)}),500


@app.route('/api/tags/<int:tag_id>',methods=['PUT'])
def update_tag(tag_id):
    data=request.get_json()
    if not data or data.get('password')!=THRESHOLD_PASSWORD:
        return jsonify({'error':'Unauthorized'}),401
    try:
        conn=sqlite3.connect(DB_PATH,isolation_level=None)
        conn.execute("UPDATE tags SET name=?,address=?,data_type=?,units=?,description=?,alarm_enabled=?,alarm_low=?,alarm_high=?,alarm_message=?,scale_factor=?,scale_offset=?,enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (data['name'],data['address'],data.get('data_type','REAL'),data.get('units',''),data.get('description',''),
             data.get('alarm_enabled',0),data.get('alarm_low'),data.get('alarm_high'),data.get('alarm_message',''),
             data.get('scale_factor',1.0),data.get('scale_offset',0.0),
             1 if data.get('enabled',True) else 0,tag_id))
        conn.row_factory=sqlite3.Row
        row=conn.execute("SELECT * FROM tags WHERE id=?",(tag_id,)).fetchone()
        conn.close()
        if not row: return jsonify({'error':'Not found'}),404
        return jsonify(dict(row))
    except Exception as e:
        return jsonify({'error':str(e)}),500


@app.route('/api/tags/<int:tag_id>',methods=['DELETE'])
def delete_tag(tag_id):
    data=request.get_json()
    if not data or data.get('password')!=THRESHOLD_PASSWORD:
        return jsonify({'error':'Unauthorized'}),401
    try:
        conn=sqlite3.connect(DB_PATH,isolation_level=None)
        conn.execute("DELETE FROM tags WHERE id=?",(tag_id,))
        conn.close(); return jsonify({'ok':True})
    except Exception as e:
        return jsonify({'error':str(e)}),500


if __name__=='__main__':
    app.run(host=settings.DASHBOARD_HOST,port=settings.DASHBOARD_PORT)

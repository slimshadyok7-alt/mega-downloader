"""
MEGA VPN Auto-Rotating Downloader - Native Android App (Kivy)
Run from Termux:  python main.py
Compile to APK:   buildozer android debug
"""

import os
import re
import json
import time
import queue
import threading
import subprocess
import requests
from pathlib import Path

from kivy.config import Config
Config.set('kivy', 'log_level', 'info')
Config.set('graphics', 'width', '400')
Config.set('graphics', 'height', '700')

from kivymd.app import MDApp
from kivy.lang import Builder
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.popup import Popup
from kivy.properties import StringProperty, NumericProperty, BooleanProperty, ListProperty, ObjectProperty
from kivy.clock import Clock
from kivy.utils import platform
from kivymd.uix.button import MDRaisedButton, MDFlatButton, MDRectangleFlatButton
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.label import MDLabel
from kivymd.uix.textfield import MDTextField
from kivymd.uix.progressbar import MDProgressBar
from kivymd.uix.list import OneLineListItem, ThreeLineListItem, TwoLineListItem, IconLeftWidget
from kivymd.uix.dialog import MDDialog
from kivymd.uix.card import MDCard
from kivymd.uix.snackbar import Snackbar

if platform == 'android':
    from plyer import storagepath
    from android.permissions import request_permissions, Permission
    request_permissions([
        Permission.WRITE_EXTERNAL_STORAGE,
        Permission.READ_EXTERNAL_STORAGE
    ])

# ─── CONFIG ──────────────────────────────────
DOWNLOAD_DIR = '/sdcard/Download/MEGA_Downloads'
VPN_DIR = os.path.expanduser('~/.mega_vpn_configs')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(VPN_DIR, exist_ok=True)

# ─── GLOBALS ─────────────────────────────────
vpn_configs = []      # list of (hostname, speed_mbps, ovpn_path)
vpn_index = 0
openvpn_proc = None
download_jobs = []    # list of dicts for tracking
active_job = None
job_queue = queue.Queue()
download_lock = threading.Lock()

# ─── HELPERS ──────────────────────────────────

def log_msg(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def human_size(num):
    if num == 0:
        return "0 B"
    for unit in ['B','KB','MB','GB','TB']:
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"

def convert_mega_url(url):
    """Handle both new and old MEGA link formats."""
    url = url.strip()
    if 'mega.nz/file/' in url:
        url = url.replace('mega.nz/file/', 'mega.nz/#!')
        # Fix: mega.nz/#!HANDLE#KEY -> mega.nz/#!HANDLE!KEY
        parts = url.split('#')
        if len(parts) >= 3:
            url = f"{parts[0]}#!{parts[1]}!{parts[2]}"
    elif 'mega.nz/folder/' in url:
        url = url.replace('mega.nz/folder/', 'mega.nz/#F!')
        parts = url.split('#')
        if len(parts) >= 3:
            url = f"{parts[0]}#F!{parts[1]}!{parts[2]}"
    return url

def parse_mega_folder(url):
    """Use megals to get file listing."""
    try:
        result = subprocess.run(
            ["megals", convert_mega_url(url)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None, result.stderr.strip()
        lines = result.stdout.strip().split('\n')
        files = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            match = re.match(r'\[([a-zA-Z0-9_-]+)\]\s+(\d+)\s+(.+)', line)
            if match:
                is_video = any(ext in match.group(3).lower() for ext in
                    ['.mp4','.mkv','.avi','.mov','.wmv','.flv','.webm','.m4v','.3gp','.ts','.m3u8'])
                files.append({
                    'handle': match.group(1),
                    'size': int(match.group(2)),
                    'name': match.group(3),
                    'is_video': is_video
                })
        return files, None
    except Exception as e:
        return None, str(e)

def stop_vpn():
    global openvpn_proc
    if openvpn_proc and openvpn_proc.poll() is None:
        try:
            openvpn_proc.terminate()
            openvpn_proc.wait(timeout=5)
        except:
            openvpn_proc.kill()
        openvpn_proc = None
        log_msg("VPN stopped")

def start_vpn(vpn_path):
    global openvpn_proc
    stop_vpn()
    if not os.path.exists(vpn_path):
        return False
    try:
        openvpn_proc = subprocess.Popen(
            ["openvpn", "--config", vpn_path, "--daemon"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(5)
        return openvpn_proc.poll() is None
    except Exception as e:
        log_msg(f"VPN start failed: {e}")
        return False

def rotate_vpn():
    global vpn_index
    if not vpn_configs:
        return False
    vpn_index = (vpn_index + 1) % len(vpn_configs)
    _, speed, path = vpn_configs[vpn_index]
    log_msg(f"Rotating to VPN #{vpn_index+1}: {path} ({speed} Mbps)")
    return start_vpn(path)

def fetch_vpn_gate_configs(max_configs=10, min_speed=50):
    """Fetch VPN Gate server list and download fastest configs."""
    try:
        log_msg("Fetching VPN Gate server list...")
        url = "http://www.vpngate.net/api/iphone/"
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            log_msg(f"VPN Gate API returned {resp.status_code}")
            return []

        lines = resp.text.split('\n')
        servers = []
        for line in lines[1:]:  # Skip header
            if not line or line.startswith('*'):
                continue
            parts = line.split(',')
            if len(parts) < 15:
                continue
            try:
                hostname = parts[1]
                ip = parts[2]
                speed = float(parts[5]) / 1e6  # bps -> Mbps
                ping = int(parts[6])
                num_sessions = int(parts[7])
                data_usage = float(parts[8])
                ovpn_b64 = parts[14]
                servers.append({
                    'hostname': hostname,
                    'ip': ip,
                    'speed_mbps': speed,
                    'ping_ms': ping,
                    'sessions': num_sessions
                })
            except:
                continue

        # Sort by speed desc, then ping asc
        servers.sort(key=lambda s: (-s['speed_mbps'], s['ping_ms']))

        # Download top configs
        configs = []
        for i, srv in enumerate(servers[:max_configs]):
            if srv['speed_mbps'] < min_speed:
                continue
            ovpn_path = os.path.join(VPN_DIR, f"vpngate_{i+1}.ovpn")
            # We need the base64-encoded ovpn from the line, but we can also
            # use the downloadable config URL
            config_url = f"https://www.vpngate.net/api/common/getconfig?host={srv['ip']}&port=443&protocol=tcp"
            try:
                ovpn_resp = requests.get(config_url, timeout=10)
                if ovpn_resp.status_code == 200 and 'remote' in ovpn_resp.text:
                    with open(ovpn_path, 'w') as f:
                        f.write(ovpn_resp.text)
                    configs.append((srv['hostname'], srv['speed_mbps'], ovpn_path))
                    log_msg(f"  ✓ {srv['hostname']} - {srv['speed_mbps']:.0f} Mbps")
            except:
                continue

        log_msg(f"Fetched {len(configs)} VPN configs")
        return configs

    except Exception as e:
        log_msg(f"VPN Gate fetch error: {e}")
        return []

# ─── DOWNLOAD ENGINE ─────────────────────────

def download_worker(app_ref):
    """Background thread: process queue, handle quota + VPN rotation."""
    global active_job

    while True:
        job = job_queue.get()
        if job is None:
            break

        with download_lock:
            active_job = job

        job['status'] = 'downloading'
        job['progress'] = 0
        job['log'] = []
        job['speed'] = 0

        url = job['url']
        dest = job['dest_folder']
        os.makedirs(dest, exist_ok=True)

        max_retries = 20
        attempt = 0
        completed = False

        # Ensure VPN is running
        if vpn_configs and (openvpn_proc is None or openvpn_proc.poll() is not None):
            _, _, path = vpn_configs[vpn_index]
            start_vpn(path)

        while attempt < max_retries and not completed:
            attempt += 1
            job['log'].append(f"Attempt {attempt}/{max_retries}")
            Clock.schedule_once(lambda dt: app_ref.refresh_ui())

            try:
                # For folder with file selection, use the specific handle
                if job.get('handles'):
                    # Download specific files from folder
                    # megadl on a single file link
                    for handle in job['handles']:
                        file_url = f"https://mega.nz/#!{handle}"
                        proc = subprocess.Popen(
                            ["megadl", "--path", dest, file_url],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True
                        )
                        quota_hit = False
                        for line in iter(proc.stdout.readline, ''):
                            line = line.rstrip()
                            job['log'].append(line)
                            prog_match = re.search(r'\[(\d+)%\]', line)
                            if prog_match:
                                job['progress'] = int(prog_match.group(1))
                            speed_match = re.search(r'(\d[\d,]*)\s*(KB|MB|GB)/s', line)
                            if speed_match:
                                val = float(speed_match.group(1).replace(',', ''))
                                unit = speed_match.group(2)
                                if unit == 'GB':
                                    val *= 1024
                                elif unit == 'KB':
                                    val /= 1024
                                job['speed'] = val
                            if '509' in line or 'over quota' in line.lower() or 'bandwidth' in line.lower():
                                quota_hit = True
                                break
                        proc.wait()
                        if quota_hit:
                            break
                else:
                    # Full folder download
                    proc = subprocess.Popen(
                        ["megadl", "--path", dest, url],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True
                    )
                    quota_hit = False
                    for line in iter(proc.stdout.readline, ''):
                        line = line.rstrip()
                        job['log'].append(line)
                        prog_match = re.search(r'\[(\d+)%\]', line)
                        if prog_match:
                            job['progress'] = int(prog_match.group(1))
                        speed_match = re.search(r'(\d[\d,]*)\s*(KB|MB|GB)/s', line)
                        if speed_match:
                            val = float(speed_match.group(1).replace(',', ''))
                            unit = speed_match.group(2)
                            if unit == 'GB':
                                val *= 1024
                            elif unit == 'KB':
                                val /= 1024
                            job['speed'] = val
                        if '509' in line or 'over quota' in line.lower() or 'bandwidth' in line.lower():
                            quota_hit = True
                            break
                    proc.wait()

                    if proc.returncode == 0 and not quota_hit:
                        completed = True
                    elif proc.returncode == 2 or quota_hit:
                        pass  # quota hit, will retry

                if quota_hit:
                    job['log'].append("⚠ QUOTA HIT - rotating VPN...")
                    job['status'] = 'quota_hit'
                    Clock.schedule_once(lambda dt: app_ref.refresh_ui())

                    if vpn_configs:
                        rotate_vpn()
                        job['log'].append("VPN rotated, resuming...")
                        time.sleep(3)
                    else:
                        job['log'].append("No VPN - waiting 60s...")
                        time.sleep(60)
                else:
                    if proc.returncode == 0:
                        completed = True

            except Exception as e:
                job['log'].append(f"Error: {e}")
                time.sleep(5)

            Clock.schedule_once(lambda dt: app_ref.refresh_ui())

        if completed:
            job['status'] = 'completed'
            job['progress'] = 100
            job['log'].append("✓ DOWNLOAD COMPLETE!")
        else:
            job['status'] = 'failed'
            job['log'].append("✗ FAILED - all retries exhausted")

        Clock.schedule_once(lambda dt: app_ref.refresh_ui())

        with download_lock:
            active_job = None
        job_queue.task_done()
        log_msg(f"Job done: {job['status']}")

# ─── KIVY UI ──────────────────────────────────

KV = '''
ScreenManager:
    id: screen_manager

    MDScreen:
        name: "main"
        MDBoxLayout:
            orientation: 'vertical'
            spacing: 0

            MDTopAppBar:
                title: "MEGA Downloader"
                md_bg_color: app.theme_cls.primary_color
                specific_text_color: 1,1,1,1
                right_action_items: [["cog", lambda x: app.show_settings()]]

            MDBoxLayout:
                orientation: 'vertical'
                padding: 12
                spacing: 12

                MDCard:
                    orientation: 'vertical'
                    padding: 12
                    spacing: 8
                    size_hint_y: None
                    height: self.minimum_height
                    md_bg_color: app.theme_cls.primary_light

                    MDLabel:
                        text: "VPN Status"
                        font_style: "Caption"
                        theme_text_color: "Secondary"

                    MDBoxLayout:
                        size_hint_y: None
                        height: 36
                        spacing: 8

                        MDLabel:
                            id: vpn_label
                            text: "No VPN"
                            font_style: "Body1"

                        MDRectangleFlatButton:
                            id: vpn_btn
                            text: "Rotate"
                            on_release: app.rotate_vpn_btn()

                MDCard:
                    orientation: 'vertical'
                    padding: 12
                    spacing: 8
                    size_hint_y: None
                    height: self.minimum_height

                    MDLabel:
                        text: "MEGA Link"
                        font_style: "Caption"
                        theme_text_color: "Secondary"

                    MDTextField:
                        id: mega_url_input
                        hint_text: "Paste MEGA link here..."
                        mode: "rectangle"
                        multiline: False

                    MDBoxLayout:
                        size_hint_y: None
                        height: "40dp"
                        spacing: 8

                        MDRaisedButton:
                            id: browse_btn
                            text: "🔍 Browse Files"
                            on_release: app.browse_link()

                        MDRectangleFlatButton:
                            id: direct_btn
                            text: "⬇ Download Direct"
                            on_release: app.download_direct()

                ScrollView:
                    id: file_scroll
                    size_hint_y: 0.4
                    do_scroll_x: False

                    MDGridLayout:
                        id: file_list
                        cols: 1
                        adaptive_height: True
                        spacing: 4
                        padding: 4

                MDCard:
                    id: progress_card
                    orientation: 'vertical'
                    padding: 12
                    spacing: 4
                    size_hint_y: None
                    height: "120dp"
                    md_bg_color: [0.1, 0.1, 0.2, 1]

                    MDLabel:
                        id: status_label
                        text: "Ready"
                        font_style: "Body1"
                        theme_text_color: "Primary"

                    MDLabel:
                        id: speed_label
                        text: ""
                        font_style: "Caption"
                        theme_text_color: "Secondary"

                    MDProgressBar:
                        id: progress_bar
                        value: 0
                        max: 100

                    ScrollView:
                        size_hint_y: None
                        height: "40dp"
                        MDLabel:
                            id: log_label
                            text: ""
                            font_style: "Caption"
                            font_size: "10sp"
                            theme_text_color: "Secondary"

    MDScreen:
        name: "settings"

        MDBoxLayout:
            orientation: 'vertical'
            spacing: 0

            MDTopAppBar:
                title: "Settings"
                md_bg_color: app.theme_cls.primary_color
                specific_text_color: 1,1,1,1
                left_action_items: [["arrow-left", lambda x: app.go_home()]]

            MDBoxLayout:
                orientation: 'vertical'
                padding: 12
                spacing: 12

                MDCard:
                    orientation: 'vertical'
                    padding: 12
                    spacing: 8
                    size_hint_y: None
                    height: self.minimum_height

                    MDLabel:
                        text: "Download Location"
                        font_style: "Caption"
                        theme_text_color: "Secondary"

                    MDTextField:
                        id: download_path
                        text: "/sdcard/Download/MEGA_Downloads"
                        mode: "rectangle"
                        multiline: False
                        on_text: app.update_download_path()

                MDCard:
                    orientation: 'vertical'
                    padding: 12
                    spacing: 8
                    size_hint_y: None
                    height: self.minimum_height

                    MDLabel:
                        text: "VPN Configs"
                        font_style: "Caption"
                        theme_text_color: "Secondary"

                    MDLabel:
                        id: vpn_count_label
                        text: "0 VPN configs loaded"
                        font_style: "Body1"

                    MDRaisedButton:
                        text: "🔄 Fetch VPN Gate Configs"
                        on_release: app.fetch_vpn_configs()

                    MDRaisedButton:
                        text: "📁 Load from folder"
                        on_release: app.load_local_vpns()
'''

class MegaDownloaderApp(MDApp):
    title = "MEGA Downloader"

    def build(self):
        self.theme_cls.primary_palette = "Indigo"
        self.theme_cls.theme_style = "Dark"
        self.root = Builder.load_string(KV)
        self.current_files = []
        self.selected_handles = set()

        # Start download worker
        self.download_thread = threading.Thread(
            target=download_worker, args=(self,), daemon=True
        )
        self.download_thread.start()

        # Periodic UI refresh
        Clock.schedule_interval(self.refresh_ui, 2)

        # Auto-fetch VPN configs on start
        Clock.schedule_once(lambda dt: self.fetch_vpn_configs(), 3)

        # Load saved state
        self.load_state()

        return self.root

    def refresh_ui(self, dt=None):
        """Called by clock or worker thread to update UI."""
        global vpn_configs, vpn_index, active_job

        try:
            # VPN status
            vpn_label = self.root.get_screen('main').ids.vpn_label
            if vpn_configs:
                host, speed, _ = vpn_configs[vpn_index]
                running = openvpn_proc and openvpn_proc.poll() is None
                vpn_label.text = f"{'🟢' if running else '🔴'} {host} ({speed:.0f} Mbps)"
            else:
                vpn_label.text = "⚪ No VPN configured"

            # Active job
            status_label = self.root.get_screen('main').ids.status_label
            speed_label = self.root.get_screen('main').ids.speed_label
            prog_bar = self.root.get_screen('main').ids.progress_bar
            log_label = self.root.get_screen('main').ids.log_label

            if active_job:
                status_label.text = f"{active_job['status'].upper()}: {active_job.get('current_file', 'downloading...')}"
                prog_bar.value = active_job.get('progress', 0)
                if active_job.get('speed', 0) > 0:
                    s = active_job['speed']
                    speed_label.text = f"{s:.1f} MB/s"
                else:
                    speed_label.text = ""
                # Last 3 log lines
                log_lines = active_job.get('log', [])[-3:]
                log_label.text = '\n'.join(log_lines)
            else:
                status_label.text = "Ready"
                speed_label.text = ""
                prog_bar.value = 0
                log_label.text = ""

            # VPN count in settings
            vpn_count = self.root.get_screen('settings').ids.vpn_count_label
            vpn_count.text = f"{len(vpn_configs)} VPN configs loaded"

        except:
            pass

    def browse_link(self):
        url_input = self.root.get_screen('main').ids.mega_url_input
        url = url_input.text.strip()
        if not url:
            self.show_snackbar("Paste a MEGA link first")
            return

        url = convert_mega_url(url)

        # Check if it's a folder or file link
        if '#F!' in url or '#F%21' in url:
            files, error = parse_mega_folder(url)
            if error:
                self.show_snackbar(f"Error: {error}")
                return
            if not files:
                self.show_snackbar("No files found in folder")
                return

            self.current_files = files
            file_list = self.root.get_screen('main').ids.file_list
            file_list.clear_widgets()

            # Header with select all / download
            header = MDBoxLayout(orientation='horizontal', size_hint_y=None, height=40, spacing=8)
            header.add_widget(MDRectangleFlatButton(
                text="Select All", on_release=lambda x: self.toggle_select_all()
            ))
            header.add_widget(MDRaisedButton(
                text=f"⬇ Download Selected ({len(files)})",
                on_release=lambda x: self.download_selected()
            ))
            file_list.add_widget(header)

            for i, f in enumerate(files):
                vid_icon = "🎬" if f['is_video'] else "📄"
                short_name = f['name']
                if len(short_name) > 50:
                    short_name = short_name[:47] + "..."

                card = MDCard(
                    orientation='horizontal',
                    size_hint_y=None,
                    height=48,
                    padding=8,
                    spacing=8,
                    md_bg_color=[0.15, 0.15, 0.25, 1]
                )

                # Checkbox alternative - tap to select
                icon = IconLeftWidget(icon="checkbox-blank-circle-outline" if i not in self.selected_handles else "check-circle")
                icon.id = f"icon_{i}"
                # Store the handle reference
                card.handle = f['handle']
                card.file_index = i

                label = MDLabel(
                    text=f"{vid_icon} {short_name}",
                    font_style="Body2",
                    size_hint_x=0.7
                )
                size_label = MDLabel(
                    text=human_size(f['size']),
                    font_style="Caption",
                    size_hint_x=0.2,
                    halign="right"
                )

                card.add_widget(icon)
                card.add_widget(label)
                card.add_widget(size_label)
                card.bind(on_release=lambda c: self.toggle_file(c))

                file_list.add_widget(card)
        else:
            # Single file - just download
            self.show_snackbar("Single file link detected - press 'Download Direct'")

    def toggle_file(self, card):
        """Toggle file selection."""
        idx = card.file_index
        handle = card.handle

        if idx in self.selected_handles:
            self.selected_handles.remove(idx)
        else:
            self.selected_handles.add(idx)

        # Update icon
        icon = card.children[2]  # Get the IconLeftWidget
        if idx in self.selected_handles:
            icon.icon = "check-circle"
        else:
            icon.icon = "checkbox-blank-circle-outline"

    def toggle_select_all(self):
        if len(self.selected_handles) == len(self.current_files):
            self.selected_handles.clear()
        else:
            self.selected_handles = set(range(len(self.current_files)))

        self.refresh_file_list()

    def refresh_file_list(self):
        """Rebuild the file list with updated selection state."""
        file_list = self.root.get_screen('main').ids.file_list
        saved = self.current_files
        file_list.clear_widgets()

        header = MDBoxLayout(orientation='horizontal', size_hint_y=None, height=40, spacing=8)
        header.add_widget(MDRectangleFlatButton(
            text="Select All", on_release=lambda x: self.toggle_select_all()
        ))
        selected_count = len(self.selected_handles)
        header.add_widget(MDRaisedButton(
            text=f"⬇ Download Selected ({selected_count})",
            on_release=lambda x: self.download_selected()
        ))
        file_list.add_widget(header)

        for i, f in enumerate(self.current_files):
            vid_icon = "🎬" if f['is_video'] else "📄"
            short_name = f['name']
            if len(short_name) > 50:
                short_name = short_name[:47] + "..."

            card = MDCard(
                orientation='horizontal',
                size_hint_y=None,
                height=48,
                padding=8,
                spacing=8,
                md_bg_color=[0.15, 0.15, 0.25, 1]
            )

            icon = IconLeftWidget(icon="check-circle" if i in self.selected_handles else "checkbox-blank-circle-outline")
            card.handle = f['handle']
            card.file_index = i

            label = MDLabel(
                text=f"{vid_icon} {short_name}",
                font_style="Body2",
                size_hint_x=0.7
            )
            size_label = MDLabel(
                text=human_size(f['size']),
                font_style="Caption",
                size_hint_x=0.2,
                halign="right"
            )

            card.add_widget(icon)
            card.add_widget(label)
            card.add_widget(size_label)
            card.bind(on_release=lambda c: self.toggle_file(c))
            file_list.add_widget(card)

    def download_selected(self, *args):
        """Download selected files from the folder."""
        if not self.selected_handles:
            self.show_snackbar("No files selected")
            return

        handles = [self.current_files[i]['handle'] for i in self.selected_handles]
        names = [self.current_files[i]['name'] for i in self.selected_handles]
        dest = os.path.join(DOWNLOAD_DIR, "MEGA_Download")

        job = {
            'url': self.root.get_screen('main').ids.mega_url_input.text.strip(),
            'handles': handles,
            'file_names': names,
            'dest_folder': dest,
            'status': 'queued',
            'progress': 0,
            'log': [],
            'speed': 0
        }

        global active_job
        with download_lock:
            active_job = job

        job_queue.put(job)
        self.show_snackbar(f"Queued {len(handles)} files for download")
        self.refresh_ui()

    def download_direct(self, *args):
        """Download a single file or whole folder."""
        url = self.root.get_screen('main').ids.mega_url_input.text.strip()
        if not url:
            self.show_snackbar("Paste a MEGA link first")
            return

        url = convert_mega_url(url)
        dest = os.path.join(DOWNLOAD_DIR, "MEGA_Download")

        job = {
            'url': url,
            'handles': None,
            'dest_folder': dest,
            'status': 'queued',
            'progress': 0,
            'log': [],
            'speed': 0
        }

        global active_job
        with download_lock:
            active_job = job

        job_queue.put(job)
        self.show_snackbar("Download queued")
        self.refresh_ui()

    def rotate_vpn_btn(self):
        if vpn_configs:
            rotate_vpn()
            self.show_snackbar("VPN rotated")
            self.refresh_ui()
        else:
            self.show_snackbar("No VPN configs loaded")

    def fetch_vpn_configs(self):
        """Fetch VPN Gate configs in background thread."""
        def fetch():
            global vpn_configs, vpn_index
            configs = fetch_vpn_gate_configs(max_configs=15, min_speed=50)
            if configs:
                vpn_configs = configs
                vpn_index = 0
                _, _, path = vpn_configs[0]
                start_vpn(path)
                Clock.schedule_once(lambda dt: self.show_snackbar(f"Loaded {len(configs)} VPN configs"), 0)
            else:
                Clock.schedule_once(lambda dt: self.show_snackbar("Failed to fetch VPN configs"), 0)
            Clock.schedule_once(lambda dt: self.refresh_ui(), 0)

        threading.Thread(target=fetch, daemon=True).start()
        self.show_snackbar("Fetching VPN configs...")

    def load_local_vpns(self):
        """Load .ovpn files from the configs folder."""
        global vpn_configs, vpn_index
        configs = []
        for f in sorted(os.listdir(VPN_DIR)):
            if f.endswith('.ovpn') and f.startswith('vpngate_'):
                configs.append((f, 100, os.path.join(VPN_DIR, f)))

        if configs:
            vpn_configs = configs
            vpn_index = 0
            _, _, path = vpn_configs[0]
            start_vpn(path)
            self.show_snackbar(f"Loaded {len(configs)} local VPN configs")
        else:
            self.show_snackbar("No VPN configs found - fetch from VPN Gate first")

    def show_snackbar(self, msg):
        try:
            Snackbar(text=msg, duration=3).show()
        except:
            pass

    def show_settings(self):
        self.root.current = "settings"

    def go_home(self):
        self.root.current = "main"

    def update_download_path(self):
        global DOWNLOAD_DIR
        DOWNLOAD_DIR = self.root.get_screen('settings').ids.download_path.text
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        self.save_state()

    def save_state(self):
        state = {'download_path': DOWNLOAD_DIR}
        try:
            with open(os.path.expanduser('~/.mega_downloader_state.json'), 'w') as f:
                json.dump(state, f)
        except:
            pass

    def load_state(self):
        try:
            with open(os.path.expanduser('~/.mega_downloader_state.json')) as f:
                state = json.load(f)
                global DOWNLOAD_DIR
                DOWNLOAD_DIR = state.get('download_path', DOWNLOAD_DIR)
                self.root.get_screen('settings').ids.download_path.text = DOWNLOAD_DIR
        except:
            pass

    def on_stop(self):
        stop_vpn()
        self.save_state()


if __name__ == "__main__":
    MegaDownloaderApp().run()

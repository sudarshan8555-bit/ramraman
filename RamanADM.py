import os
import re
import sys
import time
import mimetypes
import urllib.parse
from datetime import datetime
import concurrent.futures
import requests

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QProgressBar, QFileDialog, QMessageBox,
    QSizePolicy
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QColor, QTextCursor

# ==========================================
# FORMATTING HELPERS
# ==========================================
def format_size(bytes_size):
    if bytes_size <= 0: return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0: return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0

# ==========================================
# MASTER DOWNLOAD THREAD
# ==========================================
class MasterDownloadThread(QThread):
    progress_update = pyqtSignal(int)
    log_update = pyqtSignal(str, str)
    finished_all = pyqtSignal(int, int) # success_count, failed_count
    
    def __init__(self, tasks, session_dir, completed_urls):
        super().__init__()
        self.tasks = tasks
        self.session_dir = session_dir
        self.completed_urls = completed_urls
        self.is_paused = False
        self.is_cancelled = False
        self.executor = None
        self.bytes_downloaded_tick = 0 

    def sanitize_filename(self, name):
        return re.sub(r'[\\/*?:"<>|]', "", name).strip()

    def get_unique_filepath(self, folder, filename):
        base, ext = os.path.splitext(filename)
        counter = 1
        filepath = os.path.join(folder, filename)
        while os.path.exists(filepath):
            filepath = os.path.join(folder, f"{base}({counter}){ext}")
            counter += 1
        return filepath

    def get_filename(self, url, headers, provided_name):
        if provided_name:
            clean = self.sanitize_filename(provided_name)
            if clean: return clean
        cd = headers.get('Content-Disposition')
        if cd:
            match = re.search(r'filename="?([^";]+)"?', cd)
            if match: return self.sanitize_filename(match.group(1))
        parsed = urllib.parse.urlparse(url)
        name = os.path.basename(urllib.parse.unquote(parsed.path))
        return self.sanitize_filename(name) if name else "file"

    def download_worker(self, provided_name, url):
        if url in self.completed_urls:
            return True, url, "Already downloaded"

        max_retries = 3
        display_name = provided_name if provided_name else (url[:50] + "...")
        filepath = ""
        
        for attempt in range(1, max_retries + 1):
            if self.is_paused or self.is_cancelled:
                return False, url, "Paused/Cancelled"
            
            try:
                head_req = requests.head(url, allow_redirects=True, timeout=10)
                raw_name = self.get_filename(url, head_req.headers, provided_name)
                ext = os.path.splitext(raw_name)[1]
                if not ext:
                    c_type = head_req.headers.get('Content-Type', '').split(';')[0]
                    ext = mimetypes.guess_extension(c_type) or '.bin'
                    raw_name += ext

                if not filepath:
                    filepath = self.get_unique_filepath(self.session_dir, raw_name)

                headers = {}
                mode = 'wb'
                downloaded_size = 0
                if os.path.exists(filepath):
                    downloaded_size = os.path.getsize(filepath)
                    if downloaded_size > 0:
                        headers['Range'] = f'bytes={downloaded_size}-'
                        mode = 'ab'

                with requests.get(url, headers=headers, stream=True, timeout=15) as response:
                    if response.status_code == 416:
                        self.completed_urls.add(url)
                        self.log_update.emit(f"SUCCESS: '{os.path.basename(filepath)}'", "#198754")
                        return True, url, None
                        
                    response.raise_for_status()
                    
                    if response.status_code != 206:
                        mode = 'wb' 

                    with open(filepath, mode) as f:
                        for chunk in response.iter_content(chunk_size=65536):
                            if self.is_paused or self.is_cancelled:
                                return False, url, "Paused/Cancelled"
                            if chunk:
                                f.write(chunk)
                                self.bytes_downloaded_tick += len(chunk)
                                
                self.completed_urls.add(url)
                self.log_update.emit(f"SUCCESS: '{os.path.basename(filepath)}'", "#198754")
                return True, url, None
                
            except Exception as e:
                if attempt == max_retries:
                    self.log_update.emit(f"FAILED: '{display_name}' - {str(e)}", "#dc3545")
                    return False, url, str(e)
                else:
                    self.log_update.emit(f"Retrying ({attempt}/{max_retries}): '{display_name}'", "#fd7e14")

    def run(self):
        successful = len(self.completed_urls)
        failed_items = []
        
        os.makedirs(self.session_dir, exist_ok=True)
        self.progress_update.emit(successful)

        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
        future_to_task = {
            self.executor.submit(self.download_worker, name, url): (name, url) 
            for name, url in self.tasks if url not in self.completed_urls
        }

        try:
            for future in concurrent.futures.as_completed(future_to_task):
                if self.is_cancelled or self.is_paused:
                    break
                    
                success, url, error_msg = future.result()
                if success:
                    successful += 1
                else:
                    if error_msg != "Paused/Cancelled":
                        failed_items.append((url, error_msg))

                self.progress_update.emit(successful)
        finally:
            if self.executor:
                self.executor.shutdown(wait=False, cancel_futures=True)

        if not self.is_paused and not self.is_cancelled:
            self.finished_all.emit(successful, len(failed_items))

    def pause(self):
        self.is_paused = True

    def cancel(self):
        self.is_cancelled = True


# ==========================================
# MAIN GUI APPLICATION
# ==========================================
class RamanDownloadManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Raman Download Manager")
        self.resize(850, 750)
        self.setMinimumSize(800, 650)

        self.default_download_dir = os.path.join(os.path.expanduser('~'), 'Downloads')
        self.current_session_dir = self.default_download_dir
        
        self.worker = None
        self.tasks = []
        self.completed_urls = set()
        
        # Speed Timer
        self.speed_timer = QTimer()
        self.speed_timer.timeout.connect(self.calculate_speed)
        self.speed_timer.start(500)

        self.init_ui()
        self.apply_styles()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(25, 20, 25, 20)
        main_layout.setSpacing(12)

        # 1. Title
        title_label = QLabel("⬇️ RAMAN DOWNLOAD MANAGER")
        title_label.setObjectName("title")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(title_label)

        # 2. Input Area
        lbl_input = QLabel("Paste Links Here (1 Column: Link, OR 2 Columns: Name [Tab] Link):")
        main_layout.addWidget(lbl_input)

        self.txt_input = QTextEdit()
        self.txt_input.setPlaceholderText("Paste your URLs here...\nExample:\nFile1    http://example.com/file1.zip\nhttp://example.com/file2.zip")
        main_layout.addWidget(self.txt_input, stretch=1) 

        # 3. Folder Row & Speed
        folder_layout = QHBoxLayout()
        self.btn_select_folder = QPushButton("📁 Select Folder")
        self.btn_select_folder.clicked.connect(self.select_folder)
        
        self.btn_create_folder = QPushButton("🕒 Create Timestamp Folder")
        self.btn_create_folder.clicked.connect(self.create_timestamp_folder)

        self.lbl_folder_path = QLabel(f"Save Path: {self.current_session_dir}")
        self.lbl_folder_path.setObjectName("pathLabel")

        self.lbl_speed = QLabel("Speed: 0.00 B/s")
        self.lbl_speed.setObjectName("speedLabel")
        self.lbl_speed.setAlignment(Qt.AlignmentFlag.AlignRight)

        folder_layout.addWidget(self.btn_select_folder)
        folder_layout.addWidget(self.btn_create_folder)
        folder_layout.addWidget(self.lbl_folder_path, stretch=1)
        folder_layout.addWidget(self.lbl_speed)
        main_layout.addLayout(folder_layout)

        # 4. Global Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("Files Completed: %v / %m")
        self.progress_bar.setFixedHeight(40)
        main_layout.addWidget(self.progress_bar)

        # 5. Actions Row
        action_layout = QHBoxLayout()
        expand_policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        self.btn_reset = QPushButton("🔄 Reset")
        self.btn_reset.clicked.connect(self.reset_app)
        self.btn_reset.setObjectName("secondaryButton")
        self.btn_reset.setSizePolicy(expand_policy)
        self.btn_reset.setMinimumHeight(40)

        self.btn_open_folder = QPushButton("📂 Open Folder")
        self.btn_open_folder.clicked.connect(self.open_folder)
        self.btn_open_folder.setObjectName("secondaryButton")
        self.btn_open_folder.setSizePolicy(expand_policy)
        self.btn_open_folder.setMinimumHeight(40)

        self.btn_pause = QPushButton("⏸ Pause")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setSizePolicy(expand_policy)
        self.btn_pause.setMinimumHeight(40)

        self.btn_start = QPushButton("🚀 START")
        self.btn_start.clicked.connect(self.start_download)
        self.btn_start.setObjectName("primaryButton")
        self.btn_start.setSizePolicy(expand_policy)
        self.btn_start.setMinimumHeight(40)

        action_layout.addWidget(self.btn_reset)
        action_layout.addWidget(self.btn_open_folder)
        action_layout.addWidget(self.btn_pause)
        action_layout.addWidget(self.btn_start)
        main_layout.addLayout(action_layout)

        # 6. Log Area
        lbl_log = QLabel("Activity Log:")
        main_layout.addWidget(lbl_log)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setObjectName("logConsole")
        main_layout.addWidget(self.txt_log, stretch=1) 

    def apply_styles(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #f8f9fa; }
            QLabel { color: #212529; font-size: 13px; font-weight: bold; }
            
            QLabel#title { 
                color: #0d6efd; 
                font-size: 20px; 
                font-family: 'Arial Black', 'Segoe UI Black', Impact, sans-serif;
                font-weight: 900;
                letter-spacing: 2px;
                margin-bottom: 5px; 
            }
            
            QLabel#pathLabel { color: #6c757d; font-style: italic; font-weight: normal; }
            QLabel#speedLabel { color: #198754; font-weight: bold; }
            
            QTextEdit {
                background-color: #ffffff; color: #212529; border: 1px solid #ced4da;
                border-radius: 6px; padding: 10px; font-family: 'Consolas', monospace; font-size: 13px;
            }
            QTextEdit#logConsole { background-color: #f1f3f5; font-size: 12px; }
            
            QPushButton {
                background-color: #e9ecef; color: #495057; border: 1px solid #ced4da;
                border-radius: 6px; padding: 6px 12px; font-weight: bold; font-size: 14px;
            }
            QPushButton:hover { background-color: #dee2e6; }
            QPushButton:disabled { background-color: #e9ecef; color: #adb5bd; border-color: #e9ecef; }
            
            QPushButton#primaryButton { background-color: #0d6efd; color: white; border: none; }
            QPushButton#primaryButton:hover { background-color: #0b5ed7; }
            QPushButton#primaryButton:disabled { background-color: #a5c8fd; color: #ffffff; }
            
            QPushButton#secondaryButton { border: 2px solid #6c757d; background-color: transparent; color: #495057; }
            QPushButton#secondaryButton:hover { background-color: rgba(108, 117, 125, 0.1); }
            
            QProgressBar {
                background-color: #e9ecef; border: 1px solid #ced4da; border-radius: 6px;
                text-align: center; color: #212529; font-weight: bold;
                height: 40px; 
            }
            QProgressBar::chunk { background-color: #198754; border-radius: 5px; }
        """)

    def calculate_speed(self):
        if self.worker and self.worker.isRunning() and not self.worker.is_paused:
            bytes_downloaded = self.worker.bytes_downloaded_tick
            self.worker.bytes_downloaded_tick = 0
            speed_bps = bytes_downloaded * 2 
            self.lbl_speed.setText(f"Speed: {format_size(speed_bps)}/s")
        else:
            self.lbl_speed.setText("Speed: 0.00 B/s")

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Download Directory", self.current_session_dir)
        if folder:
            self.current_session_dir = folder
            self.lbl_folder_path.setText(f"Save Path: {self.current_session_dir}")

    def create_timestamp_folder(self):
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        new_folder = os.path.join(self.current_session_dir, timestamp)
        try:
            os.makedirs(new_folder, exist_ok=True)
            self.current_session_dir = new_folder
            self.lbl_folder_path.setText(f"Save Path: {self.current_session_dir}")
            self.append_log(f"Created target folder: {timestamp}", "#fd7e14")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not create folder:\n{str(e)}")

    def open_folder(self):
        if not self.current_session_dir or not os.path.exists(self.current_session_dir): return
        if sys.platform == "win32": os.startfile(self.current_session_dir)
        elif sys.platform == "darwin": subprocess.Popen(["open", self.current_session_dir])
        else: subprocess.Popen(["xdg-open", self.current_session_dir])

    def reset_app(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait()

        self.txt_input.clear()
        self.txt_log.clear()
        self.tasks = []
        self.completed_urls = set()
        
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(100)
        self.lbl_speed.setText("Speed: 0.00 B/s")
        
        self.current_session_dir = self.default_download_dir
        self.lbl_folder_path.setText(f"Save Path: {self.current_session_dir}")
        
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ Pause")
        self.btn_select_folder.setEnabled(True)
        self.btn_create_folder.setEnabled(True)
        self.btn_open_folder.setEnabled(True)
        self.txt_input.setEnabled(True)

        self.append_log("Application reset to default settings.", "#6c757d")

    def append_log(self, message, hex_color="#212529"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        html_msg = f"<span style='color:#6c757d'>[{timestamp}]</span> <span style='color:{hex_color}'>{message}</span>"
        self.txt_log.append(html_msg)
        
        cursor = self.txt_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.txt_log.setTextCursor(cursor)

    def parse_input_data(self):
        raw_data = self.txt_input.toPlainText().strip()
        if not raw_data: return []

        lines = raw_data.split('\n')
        tasks = []
        for line in lines:
            line = line.strip()
            if not line: continue

            parts = line.split('\t')
            if len(parts) < 2: parts = re.split(r' {2,}', line)
            
            if len(parts) >= 2:
                name, url = parts[0].strip(), parts[1].strip()
                if name.lower() in ['name', 'filename', 'title']: continue
                if url.startswith('http'): tasks.append((name, url))
            elif len(parts) == 1:
                url = parts[0].strip()
                if url.startswith('http'): tasks.append(("", url))
        return tasks

    def start_download(self):
        if not self.tasks or len(self.tasks) == len(self.completed_urls):
            self.tasks = self.parse_input_data()
            if not self.tasks:
                QMessageBox.warning(self, "No Links", "No valid links found. Please paste URLs.")
                return
            self.completed_urls = set()
            self.txt_log.clear()

        # ==========================================
        # SMART FOLDER CREATION CHECK
        # ==========================================
        if len(self.tasks) > 5 and self.current_session_dir == self.default_download_dir:
            msg = QMessageBox(self)
            msg.setWindowTitle("Organize Downloads")
            msg.setText(f"You are about to download {len(self.tasks)} files into your main Downloads folder.\n\nWould you like to automatically create a timestamped folder to keep your files organized?")
            msg.setIcon(QMessageBox.Icon.Question)
            
            btn_create = msg.addButton("Yes, Create Folder", QMessageBox.ButtonRole.ActionRole)
            btn_no = msg.addButton("No, Download Here", QMessageBox.ButtonRole.ActionRole)
            btn_cancel = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            
            msg.exec()
            
            if msg.clickedButton() == btn_cancel:
                return # Abort download
            elif msg.clickedButton() == btn_create:
                self.create_timestamp_folder()
        # ==========================================

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("⏸ Pause")
        self.btn_select_folder.setEnabled(False)
        self.btn_create_folder.setEnabled(False)
        self.txt_input.setEnabled(False)
        
        self.progress_bar.setMaximum(len(self.tasks))
        self.append_log(f"Starting queue: {len(self.tasks)} files...", "#0d6efd")

        self.worker = MasterDownloadThread(self.tasks, self.current_session_dir, self.completed_urls)
        self.worker.progress_update.connect(self.progress_bar.setValue)
        self.worker.log_update.connect(self.append_log)
        self.worker.finished_all.connect(self.on_download_finished)
        self.worker.start()

    def toggle_pause(self):
        if not self.worker: return

        if self.worker.isRunning() and not self.worker.is_paused:
            self.worker.pause()
            self.btn_pause.setText("▶ Resume")
            self.append_log("Downloads paused by user. Stopping active threads...", "#fd7e14")
            self.lbl_speed.setText("Speed: 0.00 B/s")
        else:
            self.btn_pause.setText("⏸ Pause")
            self.append_log("Resuming downloads using HTTP Range headers...", "#0d6efd")
            
            self.worker = MasterDownloadThread(self.tasks, self.current_session_dir, self.completed_urls)
            self.worker.progress_update.connect(self.progress_bar.setValue)
            self.worker.log_update.connect(self.append_log)
            self.worker.finished_all.connect(self.on_download_finished)
            self.worker.start()

    def on_download_finished(self, success_count, failed_count):
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ Pause")
        self.btn_select_folder.setEnabled(True)
        self.btn_create_folder.setEnabled(True)
        self.txt_input.setEnabled(True)
        
        self.lbl_speed.setText("Speed: 0.00 B/s")
        
        log_file_path = os.path.join(self.current_session_dir, "download_summary.txt")
        try:
            with open(log_file_path, "w", encoding="utf-8") as lf:
                lf.write(f"Session Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                lf.write(f"Total Attempted: {len(self.tasks)}\nSuccess: {success_count}\nFailed: {failed_count}\n")
            self.append_log("Session complete. Report saved.", "#198754")
        except: pass

        if failed_count > 0:
            QMessageBox.warning(self, "Finished with Errors", f"Finished with {failed_count} errors. Check the log.")
        else:
            QMessageBox.information(self, "Complete", "All files downloaded successfully!")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = RamanDownloadManager()
    window.show()
    sys.exit(app.exec())
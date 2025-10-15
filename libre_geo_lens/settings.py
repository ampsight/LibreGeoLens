import boto3
import os
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QFileDialog, QMessageBox

from .utils.logger import configure_logging, get_logger


logger = get_logger(__name__)


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LibreGeoLens Settings")

        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        # GeoJSON S3 Directory Setting
        self.s3_directory_label = QLabel("Default GeoJSON S3 Directory:")
        self.s3_directory_label.setToolTip("The default S3 directory where GeoJSON files containing image outlines are stored")
        self.s3_directory_input = QLineEdit()
        self.s3_directory_input.setToolTip("Example: s3://bucket-name/path/to/geojsons/")
        self.layout.addWidget(self.s3_directory_label)
        self.layout.addWidget(self.s3_directory_input)

        # S3 Logs Directory Setting
        self.s3_logs_directory_label = QLabel("S3 Logs Directory:")
        self.s3_logs_directory_label.setToolTip("Optional: S3 directory where logs and chips will be backed up automatically")
        self.s3_logs_directory_input = QLineEdit()
        self.s3_logs_directory_input.setToolTip("Example: s3://bucket-name/logs/ (leave empty to disable S3 backup)")
        self.layout.addWidget(self.s3_logs_directory_label)
        self.layout.addWidget(self.s3_logs_directory_input)

        # Local Logs Directory Setting
        self.local_logs_directory_label = QLabel("Local Logs Directory:")
        self.local_logs_directory_label.setToolTip("Local directory where logs and image chips will be saved")
        self.local_logs_directory_input = QLineEdit()
        self.local_logs_directory_input.setToolTip("Default: ~/LibreGeoLensLogs if left empty")
        self.layout.addWidget(self.local_logs_directory_label)
        self.layout.addWidget(self.local_logs_directory_input)

        # Browse Button
        self.browse_button = QPushButton("Browse")
        self.browse_button.clicked.connect(self.browse_logs_directory)
        self.browse_button.setToolTip("Select a folder on your computer for storing logs and chips")
        self.layout.addWidget(self.browse_button)

        # Save button
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.save_settings)
        self.save_button.setToolTip("Save these settings for future sessions")
        self.layout.addWidget(self.save_button)

        self.load_settings()

        self.s3 = boto3.client('s3')

    def load_settings(self):
        """Load settings from QSettings."""
        settings = QSettings("Ampsight", "LibreGeoLens")
        self.s3_directory_input.setText(settings.value("default_s3_directory"))
        self.s3_logs_directory_input.setText(settings.value("s3_logs_directory", ""))
        self.local_logs_directory_input.setText(settings.value("local_logs_directory", ""))

    def save_settings(self):
        """Save settings to QSettings."""
        settings = QSettings("Ampsight", "LibreGeoLens")
        previous_local_dir = settings.value("local_logs_directory", "")
        settings.setValue("default_s3_directory", self.s3_directory_input.text())
        settings.setValue("s3_logs_directory", self.s3_logs_directory_input.text())
        settings.setValue("local_logs_directory", self.local_logs_directory_input.text())
        configure_logging()
        if self.local_logs_directory_input.text() != previous_local_dir:
            if self.local_logs_directory_input.text():
                logger.info("Updated local logs directory to %s", self.local_logs_directory_input.text())
            else:
                logger.info("Cleared custom logs directory; using default location")
        QMessageBox.information(self, "Settings Saved", "Settings have been saved successfully!")
        self.accept()

    def browse_logs_directory(self):
        """Open a file dialog to select a directory."""
        directory = QFileDialog.getExistingDirectory(self, "Select Logs Directory")
        if directory:
            self.local_logs_directory_input.setText(directory)

    @staticmethod
    def get_local_files(local_directory):
        """Retrieve all file paths from the local directory."""
        local_files = {}
        for root, _, files in os.walk(local_directory):
            for file in files:
                full_path = os.path.join(root, file)
                relative_path = os.path.relpath(full_path, local_directory)
                local_files[relative_path] = full_path
        return local_files

    def get_s3_files(self, bucket_name, s3_prefix):
        """Retrieve all file keys from the S3 bucket."""
        s3_files = {}
        paginator = self.s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket_name, Prefix=s3_prefix):
            if 'Contents' in page:
                for obj in page['Contents']:
                    key = obj['Key']
                    s3_files[key] = obj['ETag'].strip('"')
        return s3_files

    def upload_new_or_updated_files(self, local_files, s3_files, bucket_name, s3_prefix):
        """Upload new or updated files to the S3 bucket."""
        for relative_path, full_path in local_files.items():
            s3_key = os.path.join(s3_prefix, relative_path).replace('\\', '/')
            # Compare files by ETag (only works for non-multipart files)
            if s3_key not in s3_files:
                print(f"Uploading new file: {relative_path}")
                self.s3.upload_file(full_path, bucket_name, s3_key)
            else:
                local_etag = self.calculate_etag(full_path)
                if s3_files[s3_key] != local_etag:
                    print(f"Updating file: {relative_path}")
                    self.s3.upload_file(full_path, bucket_name, s3_key)

    def delete_removed_files(self, local_files, s3_files, bucket_name, s3_prefix):
        """Delete files from the S3 bucket that no longer exist locally."""
        for s3_key in s3_files.keys():
            relative_path = os.path.relpath(s3_key, s3_prefix)
            if relative_path not in local_files:
                print(f"Deleting file from S3: {relative_path}")
                self.s3.delete_object(Bucket=bucket_name, Key=s3_key)

    @staticmethod
    def calculate_etag(file_path):
        """Calculate the ETag for a local file."""
        import hashlib
        md5 = hashlib.md5()
        with open(file_path, 'rb') as f:
            while chunk := f.read(8192):
                md5.update(chunk)
        return md5.hexdigest()

    def sync_local_logs_dir_with_s3(self, local_directory):
        """Sync the local directory with the S3 bucket directory."""
        s3_logs_dir = self.s3_logs_directory_input.text()
        if not s3_logs_dir:
            return
        try:
            local_files = self.get_local_files(local_directory)
            bucket_name, directory_name = s3_logs_dir.split("/")[2], '/'.join(s3_logs_dir.split("/")[3:])
            s3_files = self.get_s3_files(bucket_name, directory_name)
            self.upload_new_or_updated_files(local_files, s3_files, bucket_name, directory_name)
            self.delete_removed_files(local_files, s3_files, bucket_name, directory_name)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to sync logs with S3: {str(e)}")

import os
import math
import copy
import json
import io
import base64
import uuid
import subprocess
import platform
import shutil
import datetime
from openai import OpenAI
from groq import Groq
import boto3
import tempfile
import ntpath
from PIL import Image
import markdown
import urllib.parse
import ast
import re
import requests
import litellm
import traceback

from .settings import SettingsDialog
from .db import LogsDB
from .utils import raw_image_utils as ru
from .custom_qt import (zoom_to_and_flash_feature, CustomTextBrowser, ImageDisplayWidget,
                        AreaDrawingTool, IdentifyDrawnAreaTool)

from qgis.PyQt.QtGui import QPixmap, QImage, QColor, QTextOption, QPalette
from qgis.PyQt.QtCore import (QBuffer, QByteArray, Qt, QSettings, QVariant, QSize, QTimer, QSignalBlocker,
                              QObject, QThread, pyqtSignal, pyqtSlot, QMetaObject)
from qgis.PyQt.QtWidgets import (QSizePolicy, QFileDialog, QMessageBox, QInputDialog, QComboBox, QLabel, QVBoxLayout,
                                 QPushButton, QWidget, QTextEdit, QApplication, QRadioButton, QHBoxLayout, QDockWidget,
                                 QSplitter, QListWidget, QListWidgetItem, QDialog, QTextBrowser, QCheckBox, QLineEdit,
                                 QPlainTextEdit, QDialogButtonBox, QFormLayout, QSpinBox, QDoubleSpinBox)
from qgis.core import (QgsVectorLayer, QgsRasterLayer, QgsSymbol, QgsSimpleLineSymbolLayer, QgsUnitTypes,
                       QgsRectangle, QgsWkbTypes, QgsProject, QgsGeometry, QgsMapRendererParallelJob, QgsFeature,
                       QgsField, QgsVectorFileWriter, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeatureRequest, QgsLayerTreeLayer, QgsMessageLog)


API_KEY_SENTINEL = "__LIBREGEOLENS_API_KEY__"
# QgsMessageLog.logMessage("something", "LGL")


class MLLMStreamWorker(QObject):
    chunk_received = pyqtSignal(object, object)
    stream_failed = pyqtSignal(object)
    completed = pyqtSignal(object)
    failed = pyqtSignal(object)
    done = pyqtSignal()

    def __init__(self, model, messages, base_kwargs, stream_supported,
                 parse_stream_chunk, parse_completion_response, parent=None):
        super().__init__(parent)
        self.model = model
        self.messages = messages
        self.base_kwargs = base_kwargs
        self.stream_supported = stream_supported
        self.parse_stream_chunk = parse_stream_chunk
        self.parse_completion_response = parse_completion_response
        self._cancel_requested = False

    @pyqtSlot()
    def request_cancel(self):
        self._cancel_requested = True

    def run(self):
        response_text = ""
        reasoning_text = ""
        stream_success = False
        stream_error = None
        stream_error_traceback = None
        cancelled = False

        try:
            if self.stream_supported:
                try:
                    response_stream = litellm.completion(
                        model=self.model,
                        messages=self.messages,
                        stream=True,
                        **self.base_kwargs,
                        allowed_openai_params=['reasoning_effort'] if 'reasoning_effort' in self.base_kwargs else []
                    )
                    if self._cancel_requested:
                        cancelled = True
                        if hasattr(response_stream, "close"):
                            try:
                                response_stream.close()
                            except Exception:
                                pass
                    else:
                        for chunk in response_stream:
                            if self._cancel_requested:
                                cancelled = True
                                break
                            text_chunk, reasoning_chunk = self.parse_stream_chunk(chunk)
                            if text_chunk:
                                response_text += text_chunk
                            if reasoning_chunk:
                                reasoning_text += reasoning_chunk
                            if text_chunk or reasoning_chunk:
                                self.chunk_received.emit(text_chunk, reasoning_chunk)
                        if hasattr(response_stream, "close"):
                            try:
                                response_stream.close()
                            except Exception:
                                pass
                    if not cancelled:
                        stream_success = True
                except Exception as error:
                    stream_error = error
                    stream_error_traceback = traceback.format_exc()
                    self.stream_failed.emit({
                        "error": error,
                        "traceback": stream_error_traceback,
                    })

            cancelled = cancelled or self._cancel_requested

            if cancelled:
                self.completed.emit({
                    "response_text": response_text or "",
                    "reasoning_text": reasoning_text or "",
                    "stream_success": stream_success,
                    "stream_error": stream_error,
                    "cancelled": True,
                })
                return

            if not stream_success:
                if stream_error is not None:
                    self.failed.emit({
                        "stream_error": stream_error,
                        "traceback": stream_error_traceback,
                    })
                    return
                try:
                    non_stream_response = litellm.completion(
                        model=self.model,
                        messages=self.messages,
                        **self.base_kwargs,
                        allowed_openai_params=['reasoning_effort'] if 'reasoning_effort' in self.base_kwargs else []
                    )
                except Exception as exc:
                    error_payload = {
                        "stream_error": stream_error,
                        "final_error": exc,
                        "traceback": traceback.format_exc(),
                    }
                    self.failed.emit(error_payload)
                    return

                response_text, reasoning_text = self.parse_completion_response(non_stream_response)

            self.completed.emit({
                "response_text": response_text or "",
                "reasoning_text": reasoning_text or "",
                "stream_success": stream_success,
                "stream_error": stream_error,
                "cancelled": False,
            })
        except Exception as exc:
            self.failed.emit({
                "unexpected_error": exc,
                "traceback": traceback.format_exc(),
            })
        finally:
            self.done.emit()


class ManageServicesDialog(QDialog):
    def __init__(self, parent, service_templates, service_configurations, added_models,
                 default_service_names, deduplicate_fn):
        super().__init__(parent)
        self.setWindowTitle("Manage Services")
        self.setModal(True)
        self.resize(820, 560)

        self.templates = copy.deepcopy(service_templates)
        self.default_service_names = set(default_service_names)
        self.working_configurations = copy.deepcopy(service_configurations or {})
        self.working_added_models = copy.deepcopy(added_models or {})
        self.deduplicate_fn = deduplicate_fn

        self.working_reasoning_overrides = {}
        all_service_names = set(self.working_configurations.keys()) | set(self.templates.keys())
        for name in all_service_names:
            self.working_configurations.setdefault(name, {})
            self.working_added_models.setdefault(name, [])
            config = self.working_configurations[name]
            overrides = config.get("reasoning_overrides")
            if isinstance(overrides, dict):
                cleaned = {
                    str(model): state
                    for model, state in overrides.items()
                    if state in ("force_on", "force_off")
                }
            else:
                cleaned = {}
            self.working_reasoning_overrides[name] = cleaned
            if cleaned:
                config["reasoning_overrides"] = cleaned.copy()
            else:
                config.pop("reasoning_overrides", None)

        self._syncing_reasoning_override_ui = False

        self.current_service = None
        self.result_configurations = None
        self.result_added_models = None
        self.env_var_validation_issues = {}

        main_layout = QVBoxLayout(self)
        content_layout = QHBoxLayout()
        main_layout.addLayout(content_layout)

        list_panel = QVBoxLayout()
        self.service_list = QListWidget()
        self.service_list.currentItemChanged.connect(self.on_service_changed)
        list_panel.addWidget(self.service_list)

        list_button_layout = QHBoxLayout()
        self.add_service_button = QPushButton("Add Service")
        self.add_service_button.setToolTip("Register a new LiteLLM provider")
        self.add_service_button.clicked.connect(self.add_service)
        list_button_layout.addWidget(self.add_service_button)

        self.remove_service_button = QPushButton("Remove Service")
        self.remove_service_button.setToolTip("Remove a user-defined provider")
        self.remove_service_button.clicked.connect(self.remove_service)
        list_button_layout.addWidget(self.remove_service_button)
        list_panel.addLayout(list_button_layout)

        content_layout.addLayout(list_panel, 1)

        detail_panel = QVBoxLayout()
        detail_form = QFormLayout()
        detail_panel.addLayout(detail_form)

        self.display_name_input = QLineEdit()
        self.display_name_input.setReadOnly(True)
        detail_form.addRow("Display Name:", self.display_name_input)

        self.provider_input = QLineEdit()
        self.provider_input.setPlaceholderText("e.g. openai, groq, anthropic, gemini")
        detail_form.addRow("Provider Name:", self.provider_input)

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText("Enter the API key for this provider")
        detail_form.addRow("Provider API Key:", self.api_key_input)

        self.api_base_input = QLineEdit()
        self.api_base_input.setPlaceholderText("Optional override, e.g. for vLLM endpoints")
        detail_form.addRow("API Base (optional):", self.api_base_input)

        self.streaming_checkbox = QCheckBox("Supports streaming responses")
        detail_form.addRow("Streaming Support:", self.streaming_checkbox)

        self.limit_mode_combo = QComboBox()
        self.limit_mode_combo.addItem("No limit", "none")
        self.limit_mode_combo.addItem("Max image size (MB)", "image_mb")
        self.limit_mode_combo.addItem("Max dimensions (px)", "image_px")
        self.limit_mode_combo.currentIndexChanged.connect(self.on_limit_mode_changed)
        detail_form.addRow("Image Limit:", self.limit_mode_combo)

        self.image_mb_spin = QDoubleSpinBox()
        self.image_mb_spin.setDecimals(1)
        self.image_mb_spin.setSingleStep(0.1)
        self.image_mb_spin.setRange(0.1, 10000000.0)
        self.image_mb_spin.setToolTip("Maximum encoded image size in megabytes")
        detail_form.addRow("Max size (MB):", self.image_mb_spin)

        limit_px_layout = QHBoxLayout()
        self.longest_px_spin = QSpinBox()
        self.longest_px_spin.setRange(0, 10000000)
        self.longest_px_spin.setToolTip("Maximum allowed longest side in pixels (0 disables this constraint)")
        self.shortest_px_spin = QSpinBox()
        self.shortest_px_spin.setRange(0, 1000000)
        self.shortest_px_spin.setToolTip("Maximum allowed shortest side in pixels (0 disables this constraint)")
        limit_px_layout.addWidget(QLabel("Longest:"))
        limit_px_layout.addWidget(self.longest_px_spin)
        limit_px_layout.addSpacing(12)
        limit_px_layout.addWidget(QLabel("Shortest:"))
        limit_px_layout.addWidget(self.shortest_px_spin)
        detail_form.addRow("Max dimensions (px):", limit_px_layout)

        env_label = QLabel("Extra Env Vars:")
        env_label.setToolTip("One per line in KEY=VALUE format; values override existing environment variables")
        self.env_vars_input = QPlainTextEdit()
        detail_form.addRow(env_label, self.env_vars_input)

        detail_panel.addSpacing(12)

        models_label = QLabel("Models:")
        detail_panel.addWidget(models_label)

        self.models_list = QListWidget()
        self.models_list.currentItemChanged.connect(self.update_model_buttons)
        detail_panel.addWidget(self.models_list, 1)

        override_layout = QHBoxLayout()
        self.reasoning_override_label = QLabel("Reasoning Override:")
        self.reasoning_override_combo = QComboBox()
        self.reasoning_override_combo.addItem("Auto (use LiteLLM detection)", "auto")
        self.reasoning_override_combo.addItem("Force reasoning support (use with caution)", "force_on")
        self.reasoning_override_combo.addItem("Force disable reasoning support (use with caution)", "force_off")
        self.reasoning_override_combo.currentIndexChanged.connect(self.on_reasoning_override_changed)
        self.reasoning_override_combo.setToolTip(
            "Override LiteLLM's reasoning support detection for the selected model."
        )
        override_layout.addWidget(self.reasoning_override_label)
        override_layout.addWidget(self.reasoning_override_combo)
        detail_panel.addLayout(override_layout)

        self.reasoning_override_warning = QLabel(
            "Warning: forcing reasoning support can cause API errors or unexpected behaviour if the model "
            "does not truly support reasoning."
        )
        self.reasoning_override_warning.setWordWrap(True)
        self.reasoning_override_warning.setStyleSheet("color: #b58900;")
        detail_panel.addWidget(self.reasoning_override_warning)
        self.reasoning_override_warning.setVisible(False)
        self.reasoning_override_combo.setEnabled(False)
        self.reasoning_override_label.setEnabled(False)

        model_button_layout = QHBoxLayout()
        self.add_model_button = QPushButton("Add Model")
        self.add_model_button.clicked.connect(self.add_model)
        model_button_layout.addWidget(self.add_model_button)
        self.remove_model_button = QPushButton("Remove Model")
        self.remove_model_button.clicked.connect(self.remove_selected_model)
        model_button_layout.addWidget(self.remove_model_button)
        detail_panel.addLayout(model_button_layout)

        content_layout.addLayout(detail_panel, 2)

        button_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.handle_accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

        self.on_limit_mode_changed()
        self.update_service_list()

    # -----------------

    def update_service_list(self, select_name=None):
        names = set(self.templates.keys()) | set(self.working_configurations.keys())
        ordered_names = sorted(names, key=lambda value: value.lower())

        previous_service = self.current_service
        self.service_list.blockSignals(True)
        self.service_list.clear()
        for name in ordered_names:
            status_text = "Configured" if self._is_service_configured(name) else "Missing credentials"
            item = QListWidgetItem(f"{name} ({status_text})")
            item.setData(Qt.UserRole, name)
            self.service_list.addItem(item)
        self.service_list.blockSignals(False)

        if select_name and select_name in ordered_names:
            target_name = select_name
        elif previous_service and previous_service in ordered_names:
            target_name = previous_service
        elif ordered_names:
            target_name = ordered_names[0]
        else:
            target_name = None

        if target_name:
            for index in range(self.service_list.count()):
                item = self.service_list.item(index)
                if item.data(Qt.UserRole) == target_name:
                    self.service_list.setCurrentRow(index)
                    return

        self.service_list.setCurrentRow(-1)
        self.current_service = None
        self._set_detail_widgets_enabled(False)

    def refresh_status_labels(self):
        for index in range(self.service_list.count()):
            item = self.service_list.item(index)
            name = item.data(Qt.UserRole)
            status_text = "Configured" if self._is_service_configured(name) else "Missing credentials"
            item.setText(f"{name} ({status_text})")

    def _is_service_configured(self, service_name):
        config = self.working_configurations.get(service_name, {})
        api_key = config.get("api_key")
        env_pairs = config.get("env_vars") or {}
        if api_key:
            return True

        template = self.templates.get(service_name, {})
        required_envs = [env_info for env_info in template.get("env_vars", []) if env_info.get("required", True)]
        if required_envs:
            for env_info in required_envs:
                env_name = env_info.get("name")
                if not env_name:
                    continue
                if env_pairs.get(env_name) or os.getenv(env_name):
                    continue
                return False
            return True

        return bool(env_pairs)

    def on_service_changed(self, current_item, previous_item):
        if previous_item is not None and self.current_service is not None:
            self.persist_current_service()

        if current_item is None:
            self.current_service = None
            self._set_detail_widgets_enabled(False)
            return

        service_name = current_item.data(Qt.UserRole)
        self.current_service = service_name
        self._set_detail_widgets_enabled(True)
        self.populate_service_form(service_name)

    def _set_detail_widgets_enabled(self, enabled):
        for widget in [self.provider_input, self.api_key_input, self.api_base_input,
                       self.streaming_checkbox, self.limit_mode_combo, self.image_mb_spin,
                       self.longest_px_spin, self.shortest_px_spin, self.env_vars_input,
                       self.add_model_button, self.remove_model_button, self.models_list,
                       self.reasoning_override_label, self.reasoning_override_combo,
                       self.reasoning_override_warning]:
            widget.setEnabled(enabled)
        if not enabled:
            self.reasoning_override_warning.setVisible(False)
        self._sync_reasoning_override_ui()

    def populate_service_form(self, service_name):
        template = self.templates.get(service_name, {})
        config = self.working_configurations.get(service_name, {})

        self.display_name_input.setText(service_name)

        provider = config.get("provider_name")
        if not provider:
            provider = template.get("litellm_params", {}).get("custom_llm_provider")
        if not provider:
            provider = template.get("provider_name")
        self.provider_input.setText(provider or "")

        self.api_key_input.setText(config.get("api_key", ""))
        self.api_base_input.setText(config.get("api_base", ""))
        self.streaming_checkbox.setChecked(config.get("supports_streaming",
                                                     template.get("supports_streaming", True)))

        limits = config.get("limits") if config.get("limits") else template.get("limits", {})
        self._apply_limits_to_widgets(limits)

        env_pairs = config.get("env_vars") or {}
        env_text = "\n".join(f"{key}={value}" for key, value in env_pairs.items())
        self.env_vars_input.setPlainText(env_text)

        self.remove_service_button.setEnabled(service_name not in self.default_service_names)
        self.populate_models_list(service_name)
        self.update_model_buttons()
        self.refresh_status_labels()

    def _apply_limits_to_widgets(self, limits):
        if not limits:
            self.limit_mode_combo.setCurrentIndex(self.limit_mode_combo.findData("none"))
            self.image_mb_spin.setValue(1.0)
            self.longest_px_spin.setValue(0)
            self.shortest_px_spin.setValue(0)
        elif "image_mb" in limits:
            self.limit_mode_combo.setCurrentIndex(self.limit_mode_combo.findData("image_mb"))
            try:
                value = float(limits.get("image_mb", 0))
            except (TypeError, ValueError):
                value = 0.0
            self.image_mb_spin.setValue(max(0.1, value) if value else 1.0)
        elif "image_px" in limits:
            self.limit_mode_combo.setCurrentIndex(self.limit_mode_combo.findData("image_px"))
            px_limits = limits.get("image_px", {})
            self.longest_px_spin.setValue(int(px_limits.get("longest_side", 0) or 0))
            self.shortest_px_spin.setValue(int(px_limits.get("shortest_side", 0) or 0))
        else:
            self.limit_mode_combo.setCurrentIndex(self.limit_mode_combo.findData("none"))
            self.image_mb_spin.setValue(1.0)
            self.longest_px_spin.setValue(0)
            self.shortest_px_spin.setValue(0)
        self.on_limit_mode_changed()

    def on_limit_mode_changed(self):
        mode = self.limit_mode_combo.currentData()
        self.image_mb_spin.setEnabled(mode == "image_mb")
        self.longest_px_spin.setEnabled(mode == "image_px")
        self.shortest_px_spin.setEnabled(mode == "image_px")

    def populate_models_list(self, service_name):
        self.models_list.clear()
        template = self.templates.get(service_name, {})
        base_models = template.get("models", [])
        user_models = self.working_added_models.get(service_name, [])

        for model in base_models:
            item = QListWidgetItem(model)
            base_tooltip = "Preset model from the plugin configuration"
            item.setData(Qt.UserRole, {
                "model": model,
                "removable": False,
                "base_tooltip": base_tooltip,
            })
            self._apply_reasoning_tooltip(item, service_name)
            self.models_list.addItem(item)

        for model in user_models:
            item = QListWidgetItem(model)
            base_tooltip = "User-added model"
            item.setData(Qt.UserRole, {
                "model": model,
                "removable": True,
                "base_tooltip": base_tooltip,
            })
            self._apply_reasoning_tooltip(item, service_name)
            self.models_list.addItem(item)

        if service_name == self.current_service:
            self._sync_reasoning_override_ui()

    def update_model_buttons(self):
        item = self.models_list.currentItem()
        if item is None:
            self.remove_model_button.setEnabled(False)
            self._sync_reasoning_override_ui()
            return
        data = item.data(Qt.UserRole) or {}
        self.remove_model_button.setEnabled(bool(data.get("removable")))
        self._sync_reasoning_override_ui()

    @staticmethod
    def _describe_reasoning_override(state):
        if state == "force_on":
            return "Manual override: force reasoning support"
        if state == "force_off":
            return "Manual override: disable reasoning support"
        return None

    def _apply_reasoning_tooltip(self, item, service_name):
        if item is None:
            return
        data = item.data(Qt.UserRole) or {}
        base_tooltip = data.get("base_tooltip")
        if not base_tooltip:
            base_tooltip = item.toolTip()
        model_name = data.get("model")
        overrides = self.working_reasoning_overrides.get(service_name, {}) if service_name else {}
        description = self._describe_reasoning_override(overrides.get(model_name))
        if description:
            tooltip = f"{base_tooltip}\n{description}" if base_tooltip else description
        else:
            tooltip = base_tooltip
        item.setToolTip(tooltip or "")

    def _sync_reasoning_override_ui(self):
        has_selection = bool(self.current_service and self.models_list.currentItem())
        self.reasoning_override_label.setEnabled(has_selection)
        self.reasoning_override_combo.setEnabled(has_selection)
        if not has_selection:
            self._syncing_reasoning_override_ui = True
            with QSignalBlocker(self.reasoning_override_combo):
                index = self.reasoning_override_combo.findData("auto")
                if index != -1:
                    self.reasoning_override_combo.setCurrentIndex(index)
            self._syncing_reasoning_override_ui = False
            self._update_reasoning_override_warning("auto")
            return

        item = self.models_list.currentItem()
        data = item.data(Qt.UserRole) or {}
        model_name = data.get("model")
        overrides = self.working_reasoning_overrides.get(self.current_service, {})
        state = overrides.get(model_name, "auto")
        if state not in ("force_on", "force_off"):
            state = "auto"

        self._syncing_reasoning_override_ui = True
        with QSignalBlocker(self.reasoning_override_combo):
            index = self.reasoning_override_combo.findData(state)
            if index != -1:
                self.reasoning_override_combo.setCurrentIndex(index)
        self._syncing_reasoning_override_ui = False

        self._update_reasoning_override_warning(state)
        self._apply_reasoning_tooltip(item, self.current_service)

    def _update_reasoning_override_warning(self, state):
        if state == "force_on":
            self.reasoning_override_warning.setText(
                "Warning: forcing reasoning support can cause API errors or unexpected behaviour if the model "
                "does not truly support reasoning."
            )
            self.reasoning_override_warning.setVisible(True)
            return
        if state == "force_off":
            self.reasoning_override_warning.setText(
                "Warning: forcing reasoning disable can lead to erros, missing features or degraded responses, and "
                "reasoning-capable models may still perform hidden reasoning even if tokens are suppressed."
            )
            self.reasoning_override_warning.setVisible(True)
            return
        self.reasoning_override_warning.setVisible(False)

    def on_reasoning_override_changed(self):
        if self._syncing_reasoning_override_ui or not self.current_service:
            return
        item = self.models_list.currentItem()
        if item is None:
            return
        data = item.data(Qt.UserRole) or {}
        model_name = data.get("model")
        if not model_name:
            return

        state = self.reasoning_override_combo.currentData()
        overrides = self.working_reasoning_overrides.setdefault(self.current_service, {})
        if state == "auto":
            overrides.pop(model_name, None)
        else:
            overrides[model_name] = state

        self._apply_reasoning_tooltip(item, self.current_service)
        self._update_reasoning_override_warning(state)

    def add_service(self):
        if self.current_service is not None:
            self.persist_current_service()

        display_name, ok = QInputDialog.getText(self, "Add Service", "Display name:")
        if not ok or not display_name.strip():
            return

        name = display_name.strip()
        if name in self.templates or name in self.working_configurations:
            QMessageBox.warning(self, "Service Exists", "A service with that name already exists. Choose another name.")
            return

        self.working_configurations[name] = {
            "provider_name": "",
            "api_key": "",
            "api_base": "",
            "env_vars": {},
            "limits": {},
            "supports_streaming": True,
        }
        self.working_added_models.setdefault(name, [])
        self.working_reasoning_overrides[name] = {}
        self.update_service_list(select_name=name)

    def remove_service(self):
        item = self.service_list.currentItem()
        if not item:
            return
        service_name = item.data(Qt.UserRole)
        if service_name in self.default_service_names:
            QMessageBox.information(self, "Protected Service", "Built-in services cannot be removed.")
            return
        reply = QMessageBox.question(
            self,
            "Remove Service",
            f"Remove '{service_name}' from the configuration?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        self.persist_current_service()
        self.working_configurations.pop(service_name, None)
        self.working_added_models.pop(service_name, None)
        self.env_var_validation_issues.pop(service_name, None)
        self.working_reasoning_overrides.pop(service_name, None)
        self.current_service = None
        self._set_detail_widgets_enabled(False)
        self.update_service_list()

    def add_model(self):
        if not self.current_service:
            return
        model_name, ok = QInputDialog.getText(self, "Add Model", "Enter the full model identifier:")
        if not ok or not model_name.strip():
            return
        model_name = model_name.strip()

        existing_models = [self.models_list.item(index).text() for index in range(self.models_list.count())]
        if model_name in existing_models:
            QMessageBox.information(self, "Model Exists", "That model is already listed for this provider.")
            return

        vision_check_failed = False
        vision_check_error = ""
        try:
            supports_vision = litellm.supports_vision(model=model_name)
        except Exception as exc:
            supports_vision = None
            vision_check_failed = True
            vision_check_error = str(exc).strip()

        if supports_vision is False:
            warning_text = (
                f"""LiteLLM reports "{model_name}" does not support vision.\n\n"""
                """You can add it anyway, but image requests may fail if the model truly lacks vision support."""
            )
            reply = QMessageBox.warning(
                self,
                "Vision Support Not Confirmed",
                warning_text,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
        elif vision_check_failed:
            warning_lines = [
                f"""LibreGeoLens could not verify whether "{model_name}" supports vision.\n\n""",
                """You can continue, but image requests might fail if the model does not accept images.""",
            ]
            if vision_check_error:
                warning_lines.append(f"Details: {vision_check_error}")
            reply = QMessageBox.warning(
                self,
                "Vision Support Unknown",
                "\n\n".join(warning_lines),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return

        models = self.working_added_models.setdefault(self.current_service, [])
        models.append(model_name)
        self.working_added_models[self.current_service] = self.deduplicate_fn(models)
        self.working_reasoning_overrides.setdefault(self.current_service, {}).pop(model_name, None)
        self.populate_models_list(self.current_service)
        self.refresh_status_labels()

    def remove_selected_model(self):
        item = self.models_list.currentItem()
        if item is None:
            return
        data = item.data(Qt.UserRole) or {}
        if not data.get("removable"):
            QMessageBox.information(self, "Cannot Remove", "Preset models cannot be removed.")
            return
        model_name = data.get("model")
        models = self.working_added_models.get(self.current_service, [])
        if model_name in models:
            models.remove(model_name)
            self.working_added_models[self.current_service] = self.deduplicate_fn(models)
            self.working_reasoning_overrides.setdefault(self.current_service, {}).pop(model_name, None)
            self.populate_models_list(self.current_service)

    def persist_current_service(self):
        if not self.current_service:
            return
        config = self.working_configurations.setdefault(self.current_service, {})
        template = self.templates.get(self.current_service, {})

        provider_name = self.provider_input.text().strip()
        template_provider = template.get("litellm_params", {}).get("custom_llm_provider")
        if not template_provider:
            template_provider = template.get("provider_name")

        if self.current_service in self.default_service_names:
            if provider_name and provider_name != (template_provider or ""):
                config["provider_name"] = provider_name
            else:
                config.pop("provider_name", None)
        else:
            if provider_name:
                config["provider_name"] = provider_name
            else:
                config.pop("provider_name", None)

        api_key = self.api_key_input.text().strip()
        if api_key:
            config["api_key"] = api_key
        else:
            config.pop("api_key", None)

        api_base = self.api_base_input.text().strip()
        if api_base:
            config["api_base"] = api_base
        else:
            config.pop("api_base", None)

        supports_streaming = self.streaming_checkbox.isChecked()
        template_streaming = template.get("supports_streaming", True)
        if self.current_service in self.default_service_names:
            if supports_streaming != template_streaming:
                config["supports_streaming"] = supports_streaming
            else:
                config.pop("supports_streaming", None)
        else:
            if not supports_streaming:
                config["supports_streaming"] = False
            else:
                config.pop("supports_streaming", None)

        limits = self._collect_limits()
        template_limits = template.get("limits", {})
        if limits and limits != template_limits:
            config["limits"] = limits
        else:
            config.pop("limits", None)

        env_vars, invalid_lines = self._collect_env_vars()
        if env_vars:
            config["env_vars"] = env_vars
        else:
            config.pop("env_vars", None)

        overrides = self.working_reasoning_overrides.get(self.current_service, {})
        valid_overrides = {
            model: state
            for model, state in overrides.items()
            if state in ("force_on", "force_off")
        }
        self.working_reasoning_overrides[self.current_service] = valid_overrides.copy()
        if valid_overrides:
            config["reasoning_overrides"] = valid_overrides
        else:
            config.pop("reasoning_overrides", None)

        if invalid_lines:
            self.env_var_validation_issues[self.current_service] = invalid_lines
        else:
            self.env_var_validation_issues.pop(self.current_service, None)

        self.refresh_status_labels()

    def _collect_limits(self):
        mode = self.limit_mode_combo.currentData()
        if mode == "image_mb":
            value = float(self.image_mb_spin.value())
            if value > 0:
                return {"image_mb": value}
        elif mode == "image_px":
            longest = int(self.longest_px_spin.value())
            shortest = int(self.shortest_px_spin.value())
            limits = {}
            if longest > 0:
                limits["longest_side"] = longest
            if shortest > 0:
                limits["shortest_side"] = shortest
            if limits:
                return {"image_px": limits}
        return {}

    def _collect_env_vars(self):
        env_text = self.env_vars_input.toPlainText()
        env_pairs = {}
        invalid_lines = []
        for raw_line in env_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if "=" not in line:
                invalid_lines.append(raw_line)
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                invalid_lines.append(raw_line)
                continue
            env_pairs[key] = value
        return env_pairs, invalid_lines

    def handle_accept(self):
        self.persist_current_service()

        if self.env_var_validation_issues:
            messages = []
            for service_name, lines in self.env_var_validation_issues.items():
                formatted = "; ".join(line.strip() for line in lines if line.strip())
                messages.append(f"{service_name}: {formatted}")
            QMessageBox.warning(
                self,
                "Invalid Env Vars",
                "Fix invalid environment variable entries before saving:\n" + "\n".join(messages)
            )
            return

        missing_providers = []
        for name in set(self.templates.keys()) | set(self.working_configurations.keys()):
            config = self.working_configurations.get(name, {})
            provider = config.get("provider_name")
            if not provider:
                template = self.templates.get(name, {})
                provider = template.get("litellm_params", {}).get("custom_llm_provider")
                if not provider:
                    provider = template.get("provider_name")
            if not provider:
                missing_providers.append(name)

        if missing_providers:
            QMessageBox.warning(
                self,
                "Missing Provider",
                "Set the LiteLLM provider name for: " + ", ".join(missing_providers)
            )
            return

        self.result_configurations = {
            name: config for name, config in self.working_configurations.items() if config
        }
        self.result_added_models = {
            name: models for name, models in self.working_added_models.items() if models
        }
        self.accept()

class LibreGeoLensDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super(LibreGeoLensDockWidget, self).__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()

        # ----------------
        # ----------------

        self.current_chat_id = None
        self.conversation = []
        self.rendered_interactions = []
        self.active_streams = {}
        self.help_dialog = None
        self.info_dialog = None

        settings = QSettings("Ampsight", "LibreGeoLens")

        self.tracked_layers = []
        self.tracked_layers_names = []
        self.geojson_path = settings.value("geojson_path", None, type=str)
        self.cogs_dict = json.loads(settings.value("cogs_dict", "{}"))
        self.geojson_layer = None
        if self.geojson_path is not None and os.path.exists(self.geojson_path):
            self.handle_imagery_layers()

        self.logs_dir = settings.value("local_logs_directory", "")
        if not self.logs_dir:
            self.logs_dir = os.path.join(os.path.expanduser("~"), "LibreGeoLensLogs")
        os.makedirs(self.logs_dir, exist_ok=True)
        self.logs_db = LogsDB(os.path.join(self.logs_dir, "logs.db"))
        self.logs_db.initialize_database()

        self.current_highlighted_button = None
        self.area_drawing_tool = None
        self.identify_drawn_area_tool = None

        self.log_layer = self.create_log_layer()
        QgsProject.instance().addMapLayer(self.log_layer)
        self.style_geojson_layer(self.log_layer, color=(254, 178, 76))
        # There might be previous temp features (drawings)
        features_to_remove = [
            feature.id() for feature in self.log_layer.getFeatures()
            if str(feature["ImagePath"]) == "NULL"
        ]
        if features_to_remove:
            self.log_layer.startEditing()
            self.log_layer.dataProvider().deleteFeatures(features_to_remove)
            self.log_layer.commitChanges()
        self.log_layer.updateExtents()
        self.log_layer.triggerRepaint()

        # ----------------
        # ----------------

        self.setWindowTitle("LibreGeoLens")
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)

        splitter = QSplitter()
        splitter.setOrientation(Qt.Horizontal)
        splitter.setStretchFactor(0, 1)  # Sidebar
        splitter.setStretchFactor(1, 5)  # Main content
        splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # ----------------

        sidebar_widget = QWidget()
        sidebar_layout = QVBoxLayout(sidebar_widget)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)

        self.start_new_chat_button = QPushButton("Start New Chat")
        self.start_new_chat_button.clicked.connect(self.start_new_chat)
        self.start_new_chat_button.setToolTip("Create a new conversation with the MLLM")
        sidebar_layout.addWidget(self.start_new_chat_button)

        self.delete_chat_button = QPushButton("Delete Chat")
        self.delete_chat_button.clicked.connect(self.delete_chat)
        self.delete_chat_button.setToolTip("Delete the currently selected chat conversation")
        sidebar_layout.addWidget(self.delete_chat_button)
        
        self.export_chat_button = QPushButton("Export Chat")
        self.export_chat_button.clicked.connect(self.export_chat)
        self.export_chat_button.setToolTip("Export the current chat as a self-contained HTML file with images")
        sidebar_layout.addWidget(self.export_chat_button)
        
        self.open_logs_dir_button = QPushButton("Open Logs Directory")
        self.open_logs_dir_button.clicked.connect(lambda x: self.open_directory(self.logs_dir))
        self.open_logs_dir_button.setToolTip("Open the folder where chat logs and image chips are stored")
        sidebar_layout.addWidget(self.open_logs_dir_button)

        self.chat_list = QListWidget()
        self.chat_list.itemClicked.connect(self.load_chat)
        self.chat_list.currentItemChanged.connect(self.on_current_item_changed)
        # self.chat_list.setToolTip("List of saved chat conversations - click to load a chat")
        # Add spacing between items
        self.chat_list.setSpacing(3)
        sidebar_layout.addWidget(self.chat_list)

        buttons_layout = QVBoxLayout()

        button_3_layout = QHBoxLayout()
        buttons_layout.addLayout(button_3_layout)
        self.draw_area_button = QPushButton("Draw Area to Chip Imagery")
        self.draw_area_button.clicked.connect(lambda: self.highlight_button(self.draw_area_button))
        self.draw_area_button.clicked.connect(lambda: self.activate_area_drawing_tool(capture_image=True))
        self.draw_area_button.setToolTip(
            "Click to activate tool, then draw a rectangle on the map to extract a chip of that area")
        self.draw_area_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button_3_layout.addWidget(self.draw_area_button)

        button_4_layout = QHBoxLayout()
        buttons_layout.addLayout(button_4_layout)
        self.select_area_button = QPushButton("Select Area")
        self.select_area_button.clicked.connect(lambda: self.highlight_button(self.select_area_button))
        self.select_area_button.clicked.connect(self.activate_identify_drawn_area_tool)
        self.select_area_button.setToolTip(
            "Click to activate tool, then click on an orange chip outline to see it in the chat")
        self.select_area_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button_4_layout.addWidget(self.select_area_button)

        button_1_layout = QHBoxLayout()
        buttons_layout.addLayout(button_1_layout)
        self.load_geojson_button = QPushButton("Load GeoJSON")
        self.load_geojson_button.clicked.connect(self.load_geojson)
        self.load_geojson_button.setToolTip("Load GeoJSON file containing image outlines to browse and stream imagery")
        self.load_geojson_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button_1_layout.addWidget(self.load_geojson_button)

        button_2_layout = QHBoxLayout()
        buttons_layout.addLayout(button_2_layout)
        self.get_cogs_button = QPushButton("Draw Area to Stream COGs")
        self.get_cogs_button.clicked.connect(lambda: self.highlight_button(self.get_cogs_button))
        self.get_cogs_button.clicked.connect(lambda: self.activate_area_drawing_tool(capture_image=False))
        self.get_cogs_button.setToolTip(
            "Click to activate tool, then draw a rectangle on the map to load imagery within that area")
        self.get_cogs_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button_2_layout.addWidget(self.get_cogs_button)

        # Help button
        help_button_layout = QHBoxLayout()
        buttons_layout.addLayout(help_button_layout)
        self.help_button = QPushButton("Help")
        self.help_button.clicked.connect(self.show_quick_help)
        # help_button.setToolTip("Show LibreGeoLens quick guide")
        self.help_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        help_button_layout.addWidget(self.help_button)

        sidebar_layout.addLayout(buttons_layout)

        sidebar_widget.setLayout(sidebar_layout)
        splitter.addWidget(sidebar_widget)

        # ----------------

        main_content_widget = QWidget()
        main_content_layout = QVBoxLayout(main_content_widget)
        main_content_layout.setContentsMargins(0, 0, 0, 0)

        self.chat_history = CustomTextBrowser()
        self.chat_history.setOpenExternalLinks(False)  # Disable automatic link opening
        self.chat_history.anchorClicked.connect(self.handle_anchor_click)  # Connect the click event
        self.chat_history.setReadOnly(True)
        self.chat_history.setWordWrapMode(QTextOption.WordWrap)  # Enable text wrapping
        self.chat_history.setStyleSheet("background-color: #f5f5f5; border: 1px solid #ddd;")
        # self.chat_history.setToolTip("Chat history - click on image chips to highlight their location on the map")
        main_content_layout.addWidget(self.chat_history, stretch=8)

        self.chat_auto_scroll_enabled = True
        self.chat_history.verticalScrollBar().valueChanged.connect(self._on_chat_history_scroll)

        self.prompt_input = QTextEdit()
        self.prompt_input.setPlaceholderText("Type your prompt here...")
        self.prompt_input.setMinimumHeight(50)
        # self.prompt_input.setToolTip("Enter your question about the imagery here")
        main_content_layout.addWidget(self.prompt_input, stretch=1)

        self.radio_chip = QRadioButton("Send Screen Chip")
        self.radio_chip.setToolTip("Send a screenshot of what you see in QGIS (includes styling, labels, etc.)")
        self.radio_raw = QRadioButton("Send Raw Chip")
        self.radio_raw.setToolTip("Send the raw imagery data (no styling or overlays, extracting can be resource intensive)")
        self.radio_chip.setChecked(True)
        self.info_button = QPushButton("i")
        self.info_button.setFixedSize(20, 20)
        self.info_button.setToolTip("Click for information about chip types and image limits")
        self.info_button.clicked.connect(self.show_chip_info)
        
        radio_group_layout = QHBoxLayout()
        radio_group_layout.addWidget(self.radio_chip)
        radio_group_layout.addWidget(self.radio_raw)
        radio_group_layout.addWidget(self.info_button)
        radio_group_layout.addStretch()
        main_content_layout.addLayout(radio_group_layout)

        self.image_display_widget = ImageDisplayWidget(canvas=self.canvas, log_layer=self.log_layer)
        self.image_display_widget.setToolTip("Image chips to send - click to highlight on map, double-click to open full-size")
        main_content_layout.addWidget(self.image_display_widget, stretch=2)

        self.send_to_mllm_button = QPushButton("Send to MLLM")
        button_style_template = (
            "QPushButton {{"
            "padding: 10px; font-weight: 600;"
            "background-color: {enabled_color}; color: white;"
            "}}"
            "QPushButton:hover {{ background-color: {enabled_color}; }}"
            "QPushButton:pressed {{ background-color: {pressed_color}; }}"
            "QPushButton:disabled {{ background-color: {disabled_color}; color: #F2F2F2; }}"
        )
        self.send_to_mllm_button.setStyleSheet(
            button_style_template.format(
                enabled_color="#2E7D32",
                pressed_color="#2E7D32",
                disabled_color="#A5D6A7",
            )
        )
        self.send_to_mllm_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.send_to_mllm_button.setMinimumHeight(42)
        self.send_to_mllm_button.clicked.connect(self.send_to_mllm_fn)
        self.send_to_mllm_button.setToolTip("Send your prompt and selected image chips to the Multimodal Large Language Model")

        send_button_row = QHBoxLayout()
        send_button_row.setSpacing(12)
        send_button_row.addWidget(self.send_to_mllm_button, stretch=1)

        self.cancel_mllm_button = QPushButton("Cancel")
        self.cancel_mllm_button.setEnabled(False)
        self.cancel_mllm_button.setToolTip("Cancel the current Send to MLLM request")
        self.cancel_mllm_button.clicked.connect(self.cancel_active_mllm_request)
        self.cancel_mllm_button.setStyleSheet(
            button_style_template.format(
                enabled_color="#C62828",
                pressed_color="#C62828",
                disabled_color="#EF9A9A",
            )
        )
        self.cancel_mllm_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.cancel_mllm_button.setMinimumHeight(42)
        send_button_row.addWidget(self.cancel_mllm_button, stretch=1)

        main_content_layout.addLayout(send_button_row)

        self.service_templates = {
            "OpenAI": {
                "litellm_params": {"custom_llm_provider": "openai"},
                "models": [
                    "gpt-4o-2024-08-06",
                    "gpt-4o-mini-2024-07-18",
                    "gpt-4o",
                    "gpt-4o-mini"
                ],
                "limits": {
                    "image_px": {
                        "longest_side": 2048,
                        "shortest_side": 768
                    }
                },
                "env_vars": [
                    {"name": "OPENAI_API_KEY", "param": "api_key", "required": True},
                    {"name": "OPENAI_API_BASE", "param": "api_base", "required": False},
                    {"name": "OPENAI_ORG_ID", "param": "organization", "required": False}
                ],
                "supports_streaming": True
            },
            "Groq": {
                "litellm_params": {"custom_llm_provider": "groq"},
                "models": [
                    "meta-llama/llama-4-maverick-17b-128e-instruct",
                    "meta-llama/llama-4-scout-17b-16e-instruct"
                ],
                "limits": {
                    "image_mb": 4
                },
                "env_vars": [
                    {"name": "GROQ_API_KEY", "param": "api_key", "required": True}
                ],
                "supports_streaming": True
            },
            "Anthropic": {
                "litellm_params": {"custom_llm_provider": "anthropic"},
                "models": [
                    "claude-3-5-sonnet-20241022",
                    "claude-3-haiku-20240307"
                ],
                "limits": {
                    "image_px": {
                        "longest_side": 8000,
                        "shortest_side": 8000
                    }
                },
                "env_vars": [
                    {"name": "ANTHROPIC_API_KEY", "param": "api_key", "required": True}
                ],
                "supports_streaming": True
            },
            "Google AI Studio": {
                "litellm_params": {"custom_llm_provider": "gemini"},
                "models": [
                    "gemini-2.5-pro"
                ],
                "limits": {},  # no downscaling but bigger images are chipped
                "env_vars": [
                    {"name": "GEMINI_API_KEY", "param": "api_key", "required": True}
                ],
                "supports_streaming": True
            }
        }

        self.service_configurations = self.load_service_configurations()
        self._managed_env_keys = set()
        self.added_models = self.load_added_models()
        self.supported_api_clients = self.build_supported_api_clients()
        self.apply_service_env_overrides()
        self.available_api_clients = {}

        api_model_layout = QVBoxLayout()

        self.manage_services_button = QPushButton("Manage MLLM Services")
        self.manage_services_button.setToolTip("Configure LiteLLM providers, credentials, limits, and models")
        self.manage_services_button.clicked.connect(self.open_manage_services_dialog)
        api_model_layout.addWidget(self.manage_services_button)

        self.api_label = QLabel("MLLM Service:")
        api_model_layout.addWidget(self.api_label)

        self.api_selection = QComboBox()
        self.api_selection.currentIndexChanged.connect(self.update_model_choices)
        self.api_selection.setToolTip("Select the MLLM service provider (requires API key in QGIS settings)")
        api_model_layout.addWidget(self.api_selection)

        self.model_label = QLabel("MLLM Model:")
        api_model_layout.addWidget(self.model_label)
        self.model_selection = QComboBox()
        self.model_selection.setToolTip("Select the specific multimodal model to use for analysis")
        api_model_layout.addWidget(self.model_selection)
        self.model_selection.currentIndexChanged.connect(self.update_reasoning_controls_state)

        self.reasoning_effort_label = QLabel("Reasoning Effort:")
        self.reasoning_effort_label.setToolTip("Control the reasoning effort for supported models")
        self.reasoning_effort_combo = QComboBox()
        self.reasoning_effort_combo.setToolTip("Select the amount of reasoning effort to request")
        self.reasoning_effort_combo.addItem("Low", "low")
        self.reasoning_effort_combo.addItem("Medium", "medium")
        self.reasoning_effort_combo.addItem("High", "high")
        self.reasoning_effort_combo.currentIndexChanged.connect(self.update_reasoning_controls_state)
        reasoning_controls_layout = QHBoxLayout()
        reasoning_controls_layout.addWidget(self.reasoning_effort_label)
        reasoning_controls_layout.addWidget(self.reasoning_effort_combo)
        reasoning_controls_layout.addStretch()
        api_model_layout.addLayout(reasoning_controls_layout)

        self.refresh_available_api_clients()

        main_content_layout.addLayout(api_model_layout, stretch=1)

        main_content_widget.setLayout(main_content_layout)
        splitter.addWidget(main_content_widget)

        # ----------------

        self._stream_lock_targets = [
            self.start_new_chat_button,
            self.delete_chat_button,
            self.export_chat_button,
            self.open_logs_dir_button,
            self.draw_area_button,
            self.select_area_button,
            self.load_geojson_button,
            self.get_cogs_button,
            self.help_button,
            self.info_button,
            self.send_to_mllm_button,
            self.manage_services_button,
            self.chat_list,
            self.prompt_input,
            self.image_display_widget,
            self.api_selection,
            self.model_selection,
            self.reasoning_effort_combo,
            self.radio_chip,
            self.radio_raw,
        ]
        self._stream_locked_states = None

        # ----------------

        settings = QSettings()
        self.qgis_theme = settings.value("UI/UITheme")
        is_dark_mode = QApplication.palette().color(QPalette.Window).value() < 128
        if self.qgis_theme in ["Night Mapping", "Blend of Gray"] or (self.qgis_theme == "default" and is_dark_mode):
            chat_history_styles = """
            background-color: #2b2b2b;
            color: #ffffff;
            border: 1px solid #555;
            """
            self.text_color = "white"
            image_display_styles = """
            background-color: #2b2b2b;
            """
            self.chat_history.setStyleSheet(chat_history_styles)
            self.image_display_widget.setStyleSheet(image_display_styles)
            if os.name == "posix":
                if "darwin" == os.uname().sysname.lower():  # macOS
                    QApplication.instance().setStyleSheet("""QInputDialog, QComboBox, QPushButton, QLabel {color: #D3D3D3;}""")
                else:  # Linux
                    QApplication.instance().setStyleSheet("""QInputDialog, QComboBox, QPushButton, QLabel {color: #2b2b2b;}""")
        else:
            self.text_color = "black"

        # ----------------

        splitter.setSizes([200, 800])
        main_layout.addWidget(splitter)
        main_widget.setLayout(main_layout)

        self.setWidget(main_widget)

        # ----------------
        # ----------------

        self.load_chat_list()

        self.adjust_size_to_available_space()

        item = self.chat_list.item(self.chat_list.count() - 1)
        if item is None:
            # If there are no chats, this is likely the first time the plugin is used
            # Show the quick help and then start a new chat
            QApplication.processEvents()  # Ensure UI is fully loaded
            self.start_new_chat()

            # Show help dialog after a slight delay to allow UI to fully initialize
            QTimer.singleShot(300, lambda: self.show_quick_help(first_time=True))
        else:
            self.chat_list.setCurrentItem(item)
            self.load_chat(item)

    def closeEvent(self, event):

        if self.area_drawing_tool:
            self.area_drawing_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.canvas.unsetMapTool(self.area_drawing_tool)
            self.area_drawing_tool = None

        if self.identify_drawn_area_tool:
            if hasattr(self.identify_drawn_area_tool, "rubber_band"):
                self.identify_drawn_area_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.canvas.unsetMapTool(self.identify_drawn_area_tool)
            self.identify_drawn_area_tool = None

        features_to_remove = [
            feature.id() for feature in self.log_layer.getFeatures()
            if str(feature["ImagePath"]) == "NULL"
        ]
        if features_to_remove:
            self.log_layer.startEditing()
            self.log_layer.dataProvider().deleteFeatures(features_to_remove)
            self.log_layer.commitChanges()
        self.log_layer.updateExtents()
        self.log_layer.triggerRepaint()

        if self.current_highlighted_button:
            self.current_highlighted_button.setStyleSheet("")
            self.current_highlighted_button = None

        # Close any open dialogs
        if self.help_dialog is not None:
            self.help_dialog.close()
            self.help_dialog = None

        if self.info_dialog is not None:
            self.info_dialog.close()
            self.info_dialog = None

        self.image_display_widget.clear_images()

        # --- Call the base class implementation to properly close the widget ---
        super(LibreGeoLensDockWidget, self).closeEvent(event)

    def handle_imagery_layers(self):
        """ Handles layers during new init that might have been saved in a QGIS project and thus present before init """

        layers = QgsProject.instance().mapLayersByName("Imagery Polygons")
        if layers:
            self.geojson_layer = layers[0]
            self.tracked_layers.append(self.geojson_layer.id())
            self.tracked_layers_names.append("geojson_layer")
        else:
            self.geojson_layer = QgsVectorLayer(self.geojson_path, "Imagery Polygons", "ogr")
            QgsProject.instance().addMapLayer(self.geojson_layer)
            self.style_geojson_layer(self.geojson_layer)
            self.tracked_layers.append(self.geojson_layer.id())
            self.tracked_layers_names.append("geojson_layer")

        root = QgsProject.instance().layerTreeRoot()
        for node in root.children():
            if isinstance(node, QgsLayerTreeLayer):
                layer = node.layer()
                if layer and layer.id() in self.cogs_dict:
                    self.tracked_layers.append(layer.id())
                    self.tracked_layers_names.append(self.cogs_dict[layer.id()])

    def adjust_size_to_available_space(self):
        """ Adjust the docked widget size to fit within the QGIS interface. """
        # Get available geometry (excluding QGIS toolbars, status bars, etc.)
        available_geometry = QApplication.primaryScreen().availableGeometry()
        dock_width = available_geometry.width() * 0.2
        # Set the size of the dock
        self.setMinimumWidth(int(dock_width))
        self.resize(int(dock_width), int(available_geometry.height()))

    def _lock_ui_for_stream(self):
        if self._stream_locked_states is not None:
            return
        self._stream_locked_states = {}
        for widget in self._stream_lock_targets:
            if widget is None:
                continue
            self._stream_locked_states[widget] = widget.isEnabled()
            widget.setEnabled(False)

    def _unlock_ui_after_stream(self):
        locked_states = self._stream_locked_states
        if locked_states is None:
            self.update_reasoning_controls_state()
            return
        for widget, enabled in locked_states.items():
            if widget is None:
                continue
            widget.setEnabled(enabled)
        self._stream_locked_states = None
        self.update_reasoning_controls_state()

    def _has_active_stream(self):
        if not self.active_streams:
            return False
        # Only treat streams that still need cleanup as active to avoid blocking UI actions post-cancel
        return any(not ctx.get("ready_for_cleanup") for ctx in self.active_streams.values())

    def highlight_button(self, button):
        if self.current_highlighted_button:
            self.current_highlighted_button.setStyleSheet("")
        button.setStyleSheet("font-weight: bold;")
        self.current_highlighted_button = button

    def handle_log_layer(self):
        project = QgsProject.instance()
        project.removeMapLayer(self.log_layer.id())
        del self.log_layer
        self.log_layer = self.create_log_layer()
        QgsProject.instance().addMapLayer(self.log_layer)
        self.style_geojson_layer(self.log_layer, color=(254, 178, 76))
        if self.identify_drawn_area_tool is not None:
            self.identify_drawn_area_tool.log_layer = self.log_layer
        self.image_display_widget.log_layer = self.log_layer

    def handle_anchor_click(self, url):
        url_str = url.toString() if type(url) != str else url
        if self._has_active_stream() and not url_str.startswith("toggle://reasoning/"):
            return
        if url_str.startswith("toggle://reasoning/"):
            toggle_id = url_str.split("toggle://reasoning/", 1)[1]
            for entry in self.rendered_interactions:
                if entry.get("display_id") == toggle_id:
                    entry["reasoning_visible"] = not entry.get("reasoning_visible", False)
                    self.render_chat_history(scroll_to_end=False)
                    break
            return
        if url_str.startswith("image://"):
            image_path = urllib.parse.unquote(url_str.replace("image://", "", 1))
            if os.name == "nt":
                image_path = image_path.replace("/", "\\\\").replace("c\\", "C:\\")
            chip_id = ntpath.basename(image_path).split(".")[0].split("_screen")[0]

            # Check if image is already in display widget (use dict comprehension for efficiency)
            existing_images = {img["chip_id"]: idx for idx, img in enumerate(self.image_display_widget.images) 
                               if "chip_id" in img and img["chip_id"] is not None}
            
            if chip_id not in existing_images:
                # Add image to display widget
                self.image_display_widget.add_image(image_path)
                self.image_display_widget.images[-1]["chip_id"] = chip_id
                
                # Get chip geometry data using more efficient query
                chip = self.logs_db.fetch_chip_by_id(chip_id)
                if chip:
                    geocoords = json.loads(chip[2])
                    # Calculate bounds in one pass instead of multiple list comprehensions
                    min_x = min_y = float('inf')
                    max_x = max_y = float('-inf')
                    for lon, lat in geocoords:
                        min_x = min(min_x, lon)
                        max_x = max(max_x, lon)
                        min_y = min(min_y, lat)
                        max_y = max(max_y, lat)
                    
                    rectangle = QgsRectangle(min_x, min_y, max_x, max_y)
                    self.image_display_widget.images[-1]["rectangle_geom"] = QgsGeometry.fromRect(rectangle)

            # Use more efficient feature lookup
            request = QgsFeatureRequest().setFilterExpression(f'"ChipId" = \'{chip_id}\'')
            first_feature = next(self.log_layer.getFeatures(request), None)
            
            if first_feature:
                zoom_to_and_flash_feature(first_feature, self.canvas, self.log_layer)
            else:
                QMessageBox.warning(None, "Feature Not Found", "No feature found for the clicked chip.")

    def load_chat_list(self):
        self.chat_list.clear()
        chats = self.logs_db.fetch_all_chats()
        for chat in chats:
            chat_id, chat_summary = chat[0], chat[2]
            item = QListWidgetItem(chat_summary if chat_summary else f"New chat")
            item.setData(Qt.UserRole, chat_id)
            self.chat_list.addItem(item)

    def start_new_chat(self):
        self.current_chat_id = self.logs_db.save_chat([])
        self.conversation = []
        self.rendered_interactions = []
        self.chat_history.clear()
        self.load_chat_list()
        new_chat = self.chat_list.item(self.chat_list.count() - 1)
        self.chat_list.setCurrentItem(new_chat)
        self.load_chat(new_chat)

    def on_current_item_changed(self, current, previous):
        """Handle when user navigates with arrow keys"""
        if current:
            self.load_chat(current)
            
    def load_chat(self, item):
        chat_id = item.data(Qt.UserRole)
        self.current_chat_id = chat_id
        self.conversation = []
        self.rendered_interactions = []
        self.chat_history.clear()

        if chat_id is None:
            return

        chat_record = self.logs_db.fetch_chat_by_id(chat_id)
        if not chat_record:
            return

        interactions_sequence = json.loads(chat_record[1])

        for interaction_id in interactions_sequence:
            interaction = self.logs_db.fetch_interaction_by_id(interaction_id)
            if not interaction:
                continue

            (_, prompt, response, chip_ids, mllm_service, mllm_model, chip_modes,
             original_resolutions, actual_resolutions, reasoning_output) = interaction

            reasoning_value = reasoning_output if reasoning_output not in (None, "", "None") else None

            entry = {
                "display_id": str(interaction_id),
                "db_id": interaction_id,
                "prompt": prompt,
                "chips": [],
                "mllm_service": mllm_service,
                "mllm_model": mllm_model,
                "response": response or "",
                "response_stream": "",
                "reasoning": reasoning_value,
                "reasoning_stream": "",
                "reasoning_visible": False,
                "is_pending": False,
                "has_reasoning": bool(reasoning_value),
            }

            self.conversation.append({"role": "user", "content": [{"type": "text", "text": prompt}]})

            chip_ids_list = json.loads(chip_ids)
            try:
                chip_modes_list = ast.literal_eval(chip_modes)
            except (ValueError, SyntaxError):
                chip_modes_list = ["screen"] * len(chip_ids_list)

            try:
                original_resolutions_list = ast.literal_eval(original_resolutions) if original_resolutions else []
                actual_resolutions_list = ast.literal_eval(actual_resolutions) if actual_resolutions else []
            except (TypeError, ValueError, json.JSONDecodeError):
                original_resolutions_list = []
                actual_resolutions_list = []

            if not original_resolutions_list:
                original_resolutions_list = ["Unknown"] * len(chip_ids_list)
            if not actual_resolutions_list:
                actual_resolutions_list = ["Unknown"] * len(chip_ids_list)

            for idx, (chip_id, chip_mode) in enumerate(zip(chip_ids_list, chip_modes_list)):
                chip = self.logs_db.fetch_chip_by_id(chip_id)
                if not chip:
                    continue

                image_path = chip[1]
                normalized_path = self._normalize_path(image_path)
                display_mode = "Raw" if chip_mode == "raw" else "Screen"

                original_res = original_resolutions_list[idx] if idx < len(original_resolutions_list) else "Unknown"
                actual_res = actual_resolutions_list[idx] if idx < len(actual_resolutions_list) else "Unknown"
                resolution_text = self._format_resolution_text(original_res, actual_res)

                entry["chips"].append({
                    "image_path": normalized_path,
                    "mode_label": display_mode,
                    "resolution_text": resolution_text,
                })

                sent_image_path = image_path.replace("_screen.png", "_raw.png") if chip_mode == "raw" else image_path
                self.conversation[-1]["content"].append(
                    {"type": "local_image_path", "path": sent_image_path, "mode": chip_mode}
                )

            self.conversation.append({"role": "assistant", "content": response or ""})
            self.rendered_interactions.append(entry)

        self.render_chat_history()

    def render_chat_history(self, scroll_to_end=True):
        if not self.rendered_interactions:
            self.chat_history.clear()
            return

        html_parts = []

        for entry in self.rendered_interactions:
            user_html = markdown.markdown(f"**User:** {entry['prompt']}")
            html_parts.append(
                f'<div id="interaction-{entry["display_id"]}">{user_html}</div>'
            )

            if entry.get("is_user_only"):
                continue

            for chip in entry.get("chips", []):
                resolution_span = ""
                if chip.get("resolution_text"):
                    resolution_span = (
                        f'<span style="position: absolute; bottom: 3px; left: 5px; color: {self.text_color}; '
                        f'font-size: 10px">{chip["resolution_text"]}</span>'
                    )

                html_parts.append(
                    f'<div style="position: relative; display: inline-block;">'
                    f'    <a href="image://{chip["image_path"]}" style="text-decoration: none;">'
                    f'        <img src="file:///{chip["image_path"]}" width="75" loading="lazy"/>'
                    f'    </a>'
                    f'    <span style="position: absolute; top: 3px; right: 5px; color: {self.text_color}; font-size: 10px">'
                    f'        ({chip["mode_label"]} Chip)'
                    f'    </span>'
                    f'{resolution_span}'
                    f'</div>'
                )

            assistant_turn_start_text = f"**{entry['mllm_model']} ({entry['mllm_service']}):**"

            response_text = entry.get("response_stream") if entry.get("is_pending") else entry.get("response", "")
            response_text = response_text or ""

            if self.supports_reasoning_for_model(entry.get('mllm_service'), entry.get('mllm_model')):
                html_parts.append(
                    self._build_reasoning_section_html(entry, assistant_turn_start_text)
                )
                assistant_turn_start_text = ""

            if response_text:
                content_to_render = (
                    response_text if not assistant_turn_start_text else
                    f"{assistant_turn_start_text} {response_text}"
                )
                assistant_markdown = markdown.markdown(content_to_render)
            else:
                assistant_markdown = (
                    markdown.markdown(assistant_turn_start_text)
                    if assistant_turn_start_text else ""
                )

            if assistant_markdown:
                html_parts.append(f'<div>{assistant_markdown}</div>')

        html = ''.join(html_parts)
        scrollbar = self.chat_history.verticalScrollBar()

        if scrollbar is not None:
            previous_value = scrollbar.value()
            with QSignalBlocker(scrollbar):
                self.chat_history.setHtml(html)
                if scroll_to_end and self.chat_auto_scroll_enabled:
                    scrollbar.setValue(scrollbar.maximum())
                else:
                    # Keep the previous manual position, clamped to the new scroll bounds.
                    scrollbar.setValue(min(previous_value, scrollbar.maximum()))
        else:
            self.chat_history.setHtml(html)

    def _on_chat_history_scroll(self, _):
        scrollbar = self.chat_history.verticalScrollBar()
        if scrollbar is None:
            return

        if scrollbar.maximum() == 0:
            # No scrolling available; nothing to change.
            return

        if self.chat_auto_scroll_enabled:
            self.chat_auto_scroll_enabled = False

    @staticmethod
    def _build_reasoning_section_html(entry, assistant_intro_text):
        reasoning_text = entry.get("reasoning_stream") if entry.get("is_pending") else entry.get("reasoning")
        reasoning_text = reasoning_text or ""

        def _add_block_styles(html: str) -> str:
            block_styles = {
                "p": "margin: 0 0 8px 0;",
                "ul": "margin: 0 0 8px 18px; padding-left: 18px;",
                "ol": "margin: 0 0 8px 18px; padding-left: 18px;",
                "pre": (
                    "margin: 0 0 8px 0; padding: 8px; border-radius: 4px; "
                    "background-color: rgba(255, 255, 255, 0.35);"
                ),
                "blockquote": (
                    "margin: 0 0 8px 0; padding-left: 10px; "
                    "border-left: 3px solid rgba(79, 70, 229, 0.35);"
                ),
            }

            pattern = re.compile(r'<(p|ul|ol|pre|blockquote)([^>]*)>')

            def _inject_style(match) -> str:
                tag = match.group(1)
                attrs = match.group(2) or ""
                if "style=" in attrs:
                    return match.group(0)
                return f'<{tag}{attrs} style="{block_styles[tag]}">' \
                    if attrs else f'<{tag} style="{block_styles[tag]}">'

            return pattern.sub(_inject_style, html)

        highlight_styles = "color: #4b5563"

        toggle_styles = (
            "display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; "
            "border-radius: 999px; "
            "color: #4b5563; text-decoration: none; "
        )

        toggle_label = "Hide reasoning" if entry.get("reasoning_visible") else "Show reasoning"
        toggle_html = (
            f'<a href="toggle://reasoning/{entry["display_id"]}" '
            f'style="{toggle_styles}">{toggle_label}</a>'
        )

        assistant_html = ""
        if assistant_intro_text:
            assistant_html = markdown.markdown(assistant_intro_text)

        body_html = ""
        if entry.get("reasoning_visible"):
            if reasoning_text.strip():
                reasoning_html = markdown.markdown(reasoning_text)
            else:
                if entry.get("is_pending"):
                    reasoning_html = "<p><i>Reasoning content not yet available.</i></p>"
                else:
                    reasoning_html = "<p><i>Reasoning content not available.</i></p>"

            reasoning_html = _add_block_styles(reasoning_html)
            body_html = f'<div style="margin: 0; line-height: 1.55;">{reasoning_html}</div>'

        return (
            '<div style="margin: 4px 0;">'
            f'<p> <span> {assistant_html} </span> <span style="color: #1f254d;"> {toggle_html} </span> </p>'
            '<div class="reasoning-block" '
            f'style="{highlight_styles}">'
            f'{body_html}'
            '</div>'
            '</div>'
        )

    @staticmethod
    def _format_resolution_text(original, actual):
        if not original or original == "Unknown":
            return ""
        if not actual or actual == "Unknown" or original == actual:
            return original
        return f"{original} → {actual}"

    @staticmethod
    def _normalize_path(path_value):
        if not path_value:
            return ""
        return path_value.replace('\\', '/')

    def load_image_base64_downscale_if_needed(self, image_path, api):
        image = Image.open(image_path)
        orig_width, orig_height = image.size
        final_width, final_height = orig_width, orig_height  # Default to original size
        was_resized = False

        api_config = self.supported_api_clients.get(api, {})
        limits = api_config.get("limits", {})

        # Process pixel-based limits
        if "image_px" in limits:
            px_limits = limits["image_px"]
            longest_side_limit = px_limits.get("longest_side")
            shortest_side_limit = px_limits.get("shortest_side")

            longest = max(orig_width, orig_height)
            shortest = min(orig_width, orig_height)

            # Check if image already meets both constraints.
            if longest > longest_side_limit or shortest > shortest_side_limit:
                # Compute scale factors for each constraint.
                factor_longest = longest_side_limit / longest  # to keep the longest side within limit
                factor_shortest = shortest_side_limit / shortest  # to keep the shortest side within limit

                # Choose the smallest factor; also do not upscale (max factor = 1).
                scale_factor = min(1, factor_longest, factor_shortest)

                final_width = int(round(orig_width * scale_factor))
                final_height = int(round(orig_height * scale_factor))
                was_resized = True
                image = image.resize((final_width, final_height))

            # Save the (possibly resized) image into a buffer as PNG
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")

        # Otherwise, if the client has a file size limit in MB
        elif "image_mb" in limits:
            max_mb = limits["image_mb"]
            # Save the original image as PNG with optimization and maximum compression
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            file_size_mb = buffer.tell() / (1024 * 1024)

            # If the file size exceeds the allowed limit, predict a downscaling factor
            if file_size_mb > max_mb:
                # Predict the scaling factor assuming file size scales roughly with image area
                scaling_factor = math.sqrt(max_mb / file_size_mb)
                # Only downsample if scaling_factor < 1 (avoid upsampling)
                if scaling_factor < 1.0:
                    final_width = int(orig_width * scaling_factor)
                    final_height = int(orig_height * scaling_factor)
                    was_resized = True
                    image = image.resize((final_width, final_height))
                # Re-encode the resized image
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")

        else:
            # If no image limits are defined, just encode the image as PNG.
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")

        # Return a tuple with the base64-encoded string and dimension info
        dimensions = {
            "original": f"{orig_width}x{orig_height}", 
            "final": f"{final_width}x{final_height}",
            "was_resized": was_resized
        }
        
        return base64.b64encode(buffer.getvalue()).decode("utf-8"), dimensions

    @staticmethod
    def style_geojson_layer(geojson_layer, color=(255, 0, 0)):
        symbol = QgsSymbol.defaultSymbol(geojson_layer.geometryType())
        if symbol is not None:
            line_layer = QgsSimpleLineSymbolLayer()
            line_layer.setColor(QColor(*color))
            line_layer.setWidth(2)
            line_layer.setWidthUnit(QgsUnitTypes.RenderMillimeters)
            symbol.changeSymbolLayer(0, line_layer)
            geojson_layer.renderer().setSymbol(symbol)
        geojson_layer.triggerRepaint()

    def load_geojson(self):
        source, ok = QInputDialog.getItem(
            self.iface.mainWindow(),
            "Select Source",
            "Choose the source of GeoJSON files:",
            ["S3 Directory", "Local Machine", "Use Demo Resources"],
            0,
            False
        )
        if not ok:
            return  # User canceled
        if source == "Local Machine":
            self.load_geojson_from_local()
        elif source == "S3 Directory":
            self.load_geojson_from_s3()
        elif source == "Use Demo Resources":
            self.load_geojson_from_demo()
        settings = QSettings("Ampsight", "LibreGeoLens")
        settings.setValue("geojson_path", self.geojson_path)

    def load_geojson_from_demo(self):
        demo_geojson_path = os.path.join(self.logs_dir, "demo_imagery.geojson")
        if not os.path.exists(demo_geojson_path):
            try:
                response = requests.get("https://libre-geo-lens.s3.us-east-1.amazonaws.com/demo/demo_imagery.geojson")
                response.raise_for_status()
                with open(demo_geojson_path, "wb") as file:
                    file.write(response.content)
            except requests.RequestException as e:
                QMessageBox.critical(self.iface.mainWindow(), "Error", f"Failed to download GeoJSON: {e}")
                return
        self.geojson_path = demo_geojson_path
        self.replace_geojson_layer()

    def load_geojson_from_local(self):
        self.geojson_path, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(),
            "Select GeoJSON File",
            "",
            "GeoJSON Files (*.geojson);;All Files (*)"
        )
        if not self.geojson_path:
            return  # User canceled
        self.replace_geojson_layer()

    def load_geojson_from_s3(self):
        settings = QSettings("Ampsight", "LibreGeoLens")
        default_s3_directory = settings.value("default_s3_directory", "")

        s3_path, ok = QInputDialog.getText(
            self.iface.mainWindow(), "S3 Directory Path", "Enter the S3 directory path:", text=default_s3_directory
        )
        if not ok or not s3_path:
            return

        bucket_name, directory_name = s3_path.split("/")[2], '/'.join(s3_path.split("/")[3:])
        s3 = boto3.client('s3')
        response = s3.list_objects_v2(Bucket=bucket_name, Prefix=directory_name)
        if 'Contents' not in response:
            QMessageBox.warning(self.iface.mainWindow(), "Error", "No files found in the specified S3 directory.")
            return

        # Extract GeoJSON files and sort by timestamp (or just sort)
        geojson_files = [
            obj['Key'] for obj in response['Contents']
            if obj['Key'].endswith('.geojson')
        ]
        geojson_files.sort(reverse=True)
        if not geojson_files:
            QMessageBox.warning(self.iface.mainWindow(), "Error", "No GeoJSON files found in the S3 directory.")
            return

        # Prompt user to select a specific file if desired
        file_name, ok = QInputDialog.getItem(
            self.iface.mainWindow(),
            "Select GeoJSON File",
            "Choose a GeoJSON file. It defaults to the latest one.",
            [os.path.basename(f) for f in geojson_files],
            0,
            False
        )
        if ok:
            selected_file = os.path.join(directory_name, file_name)
        else:
            return  # User canceled

        # Download the selected file
        local_path = os.path.join(tempfile.gettempdir(), os.path.basename(selected_file))
        if not os.path.exists(local_path):
            s3.download_file(bucket_name, selected_file, local_path)
        self.geojson_path = local_path
        self.replace_geojson_layer()

    def replace_geojson_layer(self):
        if not self.geojson_path:
            QMessageBox.critical(self.iface.mainWindow(), "Error", "No GeoJSON path set.")
            return

        # Remove previously tracked layers
        project = QgsProject.instance()
        for layer_id in self.tracked_layers:
            layer = project.mapLayer(layer_id)
            if layer:
                project.removeMapLayer(layer_id)
                del layer
        self.tracked_layers.clear()
        self.tracked_layers_names.clear()

        # Load the new GeoJSON layer
        self.geojson_layer = QgsVectorLayer(self.geojson_path, "Imagery Polygons", "ogr")
        if not self.geojson_layer.isValid():
            QMessageBox.critical(self.iface.mainWindow(), "Error", "Failed to load GeoJSON layer.")
            return

        # Add the new GeoJSON layer and style it
        project.addMapLayer(self.geojson_layer)
        self.style_geojson_layer(self.geojson_layer)
        self.tracked_layers.append(self.geojson_layer.id())
        self.tracked_layers_names.append("geojson_layer")

        self.handle_log_layer()

        QMessageBox.information(self.iface.mainWindow(), "Success", "GeoJSON loaded successfully!")

    def create_log_layer(self):
        """
        Load logs.geojson from self.logs_dir if it exists.
        Otherwise, create a new in-memory log layer.
        """
        existing_layer = QgsProject.instance().mapLayersByName("Logs")
        if existing_layer:
            for layer in existing_layer:
                QgsProject.instance().removeMapLayer(layer.id())
                del layer

        logs_path = os.path.join(self.logs_dir, "logs.geojson")
        if os.path.exists(logs_path):
            layer = QgsVectorLayer(logs_path, "Logs", "ogr")
            if not layer.isValid():
                QMessageBox.warning(self, "Error", f"Failed to load {logs_path}. Creating a new log layer.")
                return self._create_memory_log_layer()
            return layer
        else:
            return self._create_memory_log_layer()

    @staticmethod
    def _create_memory_log_layer():
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "Logs", "memory")
        provider = layer.dataProvider()
        provider.addAttributes([
            QgsField("Interactions", QVariant.String),
            QgsField("ImagePath", QVariant.String),
            QgsField("ChipId", QVariant.String)
        ])
        layer.updateFields()
        return layer

    def save_logs_to_geojson(self):
        logs_path = os.path.join(self.logs_dir, "logs.geojson")
        QgsVectorFileWriter.writeAsVectorFormat(
            self.log_layer,
            logs_path,
            "utf-8",
            self.log_layer.crs(),
            "GeoJSON"
        )

    @staticmethod
    def save_image_to_buffer(image):
        """Converts the QGIS map image to a buffer in PNG format."""
        # Create a QByteArray to hold the data
        byte_array = QByteArray()
        # Create a QBuffer to wrap around the QByteArray
        buffer = QBuffer(byte_array)
        buffer.open(QBuffer.WriteOnly)
        # Convert the QImage to QPixmap and save it to the buffer
        pixmap = QPixmap.fromImage(image)
        if not pixmap.save(buffer, "PNG"):
            raise ValueError("Failed to save image to buffer")
        buffer.close()
        # Return a Python BytesIO object from the QByteArray data
        return io.BytesIO(byte_array.data())

    def activate_area_drawing_tool(self, capture_image):
        if self.area_drawing_tool:
            self.area_drawing_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)  # Clear the previous selection
        if self.identify_drawn_area_tool:
            if hasattr(self.identify_drawn_area_tool, "rubber_band"):
                self.identify_drawn_area_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.identify_drawn_area_tool = None
        self.area_drawing_tool = AreaDrawingTool(
            self.canvas,
            lambda rectangle: self.on_drawing_finished(rectangle, capture_image=capture_image)
        )
        self.canvas.setMapTool(self.area_drawing_tool)

    def on_drawing_finished(self, rectangle, capture_image):
        if not capture_image:
            self.display_cogs_within_rectangle(rectangle)
            return

        rectangle_geom = self.transform_rectangle_crs(rectangle, QgsCoordinateReferenceSystem("EPSG:4326"))

        # Add the drawn area as a temporary feature
        feature = QgsFeature(self.log_layer.fields())
        feature.setGeometry(rectangle_geom)
        chip_id = str(uuid.uuid4())  # temp uuid until the chip is saved if eventually sent to the MLLM
        feature.setAttributes([json.dumps({}), None, chip_id])

        # Capture the image within the drawn area
        image = self.capture_drawn_area(rectangle)
        self.image_display_widget.add_image(image=image)
        self.image_display_widget.images[-1]["rectangle_geom"] = rectangle_geom
        self.image_display_widget.images[-1]["chip_id"] = chip_id

        # Add the feature after capturing the area - otherwise we'll also capture the drawing
        self.log_layer.dataProvider().addFeatures([feature])
        self.log_layer.updateExtents()
        QgsProject.instance().layerTreeRoot().findLayer(self.log_layer.id()).setItemVisibilityChecked(True)
        self.log_layer.triggerRepaint()

    def transform_rectangle_crs(self, rectangle, crs_dest):
        crs_src = self.canvas.mapSettings().destinationCrs()
        transform = QgsCoordinateTransform(crs_src, crs_dest, QgsProject.instance())
        rectangle_geom = QgsGeometry.fromRect(rectangle)
        rectangle_geom.transform(transform)
        return rectangle_geom

    def capture_drawn_area(self, rectangle):
        """Captures the drawn area as an image using the input rectangle"""
        # Set the map settings extent
        settings = self.canvas.mapSettings()
        settings.setExtent(rectangle)

        # Adjust output size to match rectangle aspect ratio
        map_width, map_height = settings.outputSize().width(), settings.outputSize().height()
        aspect_ratio = rectangle.width() / rectangle.height()
        if map_width / map_height > aspect_ratio:
            # Adjust width to match height-based ratio
            new_width = int(map_height * aspect_ratio)
            settings.setOutputSize(QSize(new_width, map_height))
        else:
            # Adjust height to match width-based ratio
            new_height = int(map_width / aspect_ratio)
            settings.setOutputSize(QSize(map_width, new_height))

        image = QImage(settings.outputSize(), QImage.Format_ARGB32_Premultiplied)
        renderer = QgsMapRendererParallelJob(settings)
        renderer.start()
        renderer.waitForFinished()

        return renderer.renderedImage()

    def activate_identify_drawn_area_tool(self):
        if self.area_drawing_tool:
            self.area_drawing_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)  # Clear the previous selection
            self.area_drawing_tool = None
        if self.identify_drawn_area_tool and hasattr(self.identify_drawn_area_tool, "rubber_band"):
            self.identify_drawn_area_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.identify_drawn_area_tool = IdentifyDrawnAreaTool(self.canvas, self.log_layer, self)
        self.canvas.setMapTool(self.identify_drawn_area_tool)

    def display_cogs_within_rectangle(self, rectangle):
        """Displays only the COGs within the given rectangle on the QGIS UI
           and ensures logs and polygons layers remain on top."""
        if not self.geojson_path:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Warning",
                f"No GeoJSON has been loaded. Please load a GeoJSON file first."
            )
            return

        rectangle_geom = self.transform_rectangle_crs(rectangle, self.geojson_layer.crs())

        # Record how many layers are currently tracked
        old_count = len(self.tracked_layers)

        # Find features that intersect with the rectangle
        cogs_paths = []
        for feature in self.geojson_layer.getFeatures():
            if feature.geometry().intersects(rectangle_geom):
                remote_path = feature["remote_path"]
                if remote_path and remote_path not in self.tracked_layers_names:
                    cogs_paths.append(remote_path)

        def load_cog(remote_path):
            if remote_path.startswith("s3://"):
                cog_url = f"/vsis3/{remote_path[5:]}"
            elif remote_path.startswith("https://"):
                cog_url = f"/vsicurl/{remote_path}"
            else:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Warning",
                    f"Unsupported remote path format: {remote_path}"
                )
                return
            raster_layer = QgsRasterLayer(cog_url, remote_path.split('/')[-1], "gdal")
            if raster_layer.isValid():
                QgsProject.instance().addMapLayer(raster_layer)
                self.tracked_layers.append(raster_layer.id())
                self.cogs_dict[raster_layer.id()] = remote_path
                settings = QSettings("Ampsight", "LibreGeoLens")
                settings.setValue("cogs_dict", json.dumps(self.cogs_dict))  # Save as JSON string
                self.tracked_layers_names.append(remote_path)
            else:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Warning",
                    f"Failed to load COG: {remote_path}"
                )

        # Load corresponding COGs if it's not too many or give the option to select one to load
        if len(cogs_paths) <= 5:
            for cog_path in cogs_paths:
                load_cog(cog_path)
        else:
            options = [path.split('/')[-1] for path in cogs_paths]
            selected_option, ok = QInputDialog.getItem(
                None,
                "Select Image Outline",
                "Please draw an area that intersects with no more than 5 image outlines or select one of these to load:",
                options,
                0,
                False
            )
            if ok:
                selected_index = options.index(selected_option)
                load_cog(cogs_paths[selected_index])
            else:
                return

        # Reorder layers to ensure log layer and GeoJSON layer remain on top
        root = QgsProject.instance().layerTreeRoot()
        log_layer_node = root.findLayer(self.log_layer.id())
        geojson_layer_node = root.findLayer(self.geojson_layer.id())
        # Move the log layer to the top
        if log_layer_node:
            root.insertChildNode(0, log_layer_node.clone())
            root.removeChildNode(log_layer_node)
        # Move the GeoJSON (polygon) layer to the second position
        if geojson_layer_node:
            root.insertChildNode(1, geojson_layer_node.clone())
            root.removeChildNode(geojson_layer_node)

        # compare old vs. new count of tracked layers
        new_count = len(self.tracked_layers)
        cogs_added = new_count - old_count
        if cogs_added == 0:
            QMessageBox.information(
                self.iface.mainWindow(),
                "No COGs Found",
                "No imagery was found within the drawn rectangle or imagery already loaded."
            )
        else:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Success",
                "COGs within the rectangle have been displayed."
            )

    def open_directory(self, local_dir):
        """Open the logs directory using the default file explorer for the current OS."""
        local_dir = os.path.abspath(local_dir)
        
        try:
            if platform.system() == "Windows":
                os.startfile(local_dir)
            elif platform.system() == "Darwin":  # macOS
                subprocess.run(["open", local_dir], check=True)
            else:  # Linux and other Unix-like systems
                subprocess.run(["xdg-open", local_dir], check=True)
        except Exception as e:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Error",
                f"Failed to open logs directory: {str(e)}"
            )
    
    def show_chip_info(self):
        """Display information about chip types and image limits in a non-modal dialog."""
        info_text = """
<h3>Chip Types:</h3>
<p><b>Screen Chip:</b> A screenshot of what you see in QGIS. Includes all visible layers, labels, and styling.</p>
<p><b>Raw Chip:</b> The original imagery data extracted directly from the source (COG).
 Contains only the raw imagery without any QGIS styling or overlays. Note that extracting large chips will be resource intensive.</p>

<h3>Image Limits by MLLM Service:</h3>
<ul>
"""
        # Dynamically generate limits information from supported_api_clients
        for api_name, api_info in self.supported_api_clients.items():
            info_text += f"<li><b>{api_name}:</b><ul>"
            limits = api_info.get("limits", {})
            
            if "image_px" in limits:
                px_limits = limits["image_px"]
                info_text += f"<li>Max dimensions: {px_limits.get('longest_side')}px (longest side), {px_limits.get('shortest_side')}px (shortest side)</li>"
            
            if "image_mb" in limits:
                info_text += f"<li>Max file size: {limits['image_mb']}MB</li>"
                
            info_text += "</ul></li>"
        
        info_text += """
</ul>
<p><b>Note:</b> Images will be automatically downsampled if they exceed these limits.</p>
"""

        # Check if we already have an open info dialog
        if self.info_dialog is not None:
            # If dialog exists, just make sure it's visible and bring to front
            self.info_dialog.show()
            self.info_dialog.raise_()
            self.info_dialog.activateWindow()
            return

        # Create a new dialog
        self.info_dialog = QDialog(self)
        self.info_dialog.setWindowTitle("Chip Types and Image Limits")
        self.info_dialog.resize(500, 400)  # Set a reasonable size

        # Create layout
        layout = QVBoxLayout()
        
        # Create text browser for rich text display
        text_browser = QTextBrowser()
        text_browser.setHtml(info_text)
        layout.addWidget(text_browser)

        # Add a close button
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.info_dialog.close)
        layout.addWidget(close_button)

        self.info_dialog.setLayout(layout)

        # Handle dialog closure to reset the reference
        self.info_dialog.finished.connect(self.on_info_dialog_closed)

        # Show the dialog non-modally
        self.info_dialog.show()

    def on_info_dialog_closed(self):
        """Reset the info_dialog reference when the dialog is closed"""
        self.info_dialog = None

    def load_service_configurations(self):
        """Load persisted service overrides from QSettings."""
        settings = QSettings("Ampsight", "LibreGeoLens")
        stored = settings.value("service_configurations", "{}")
        parsed = {}
        if isinstance(stored, str):
            try:
                parsed = json.loads(stored)
            except json.JSONDecodeError:
                parsed = {}
        elif isinstance(stored, dict):
            parsed = stored
        result = {}
        if isinstance(parsed, dict):
            for service_name, config in parsed.items():
                if isinstance(config, dict):
                    result[service_name] = config
        return result

    def save_service_configurations(self):
        """Persist service overrides to QSettings."""
        settings = QSettings("Ampsight", "LibreGeoLens")
        settings.setValue("service_configurations", json.dumps(self.service_configurations or {}))

    def build_supported_api_clients(self):
        """Combine built-in templates with user overrides and custom services."""
        merged = {}
        for name, template in self.service_templates.items():
            merged[name] = self._compose_service_entry(name, template, self.service_configurations.get(name))

        for name, config in self.service_configurations.items():
            if name in merged:
                continue
            merged[name] = self._compose_service_entry(name, {}, config)

        return merged

    def _compose_service_entry(self, name, template, user_config):
        entry = copy.deepcopy(template) if template else {}
        entry.setdefault("litellm_params", {})
        entry.setdefault("models", [])
        entry.setdefault("limits", {})
        entry.setdefault("env_vars", [])
        entry.setdefault("supports_streaming", True)
        entry.setdefault("reasoning_overrides", {})
        entry["user_defined"] = not bool(template)

        user_section = copy.deepcopy(user_config) if user_config else {}
        provider_override = user_section.get("provider_name")
        if provider_override:
            entry["litellm_params"] = dict(entry.get("litellm_params", {}))
            entry["litellm_params"]["custom_llm_provider"] = provider_override

        entry["supports_streaming"] = user_section.get("supports_streaming", entry.get("supports_streaming", True))

        if user_section.get("limits"):
            entry["limits"] = copy.deepcopy(user_section["limits"])

        base_models = entry.get("models", [])
        user_base_models = user_section.get("base_models", [])
        if user_base_models:
            combined_models = self._deduplicate_preserve_order(base_models + user_base_models)
            entry["models"] = combined_models

        overrides = user_section.get("reasoning_overrides")
        if isinstance(overrides, dict):
            cleaned_overrides = {
                str(model): state
                for model, state in overrides.items()
                if state in ("force_on", "force_off")
            }
        else:
            cleaned_overrides = {}
        entry["reasoning_overrides"] = cleaned_overrides

        entry["user_config"] = user_section
        entry["provider_id"] = entry.get("litellm_params", {}).get("custom_llm_provider")
        entry["user_env_overrides"] = user_section.get("env_vars", {})
        entry["stored_api_key"] = user_section.get("api_key")
        entry["stored_api_base"] = user_section.get("api_base")
        return entry

    def apply_service_env_overrides(self):
        """Apply env var overrides defined in service configurations."""
        new_keys = set()
        for config in self.service_configurations.values():
            env_vars = config.get("env_vars") or {}
            for key, value in env_vars.items():
                if not key or value is None:
                    continue
                os.environ[key] = value
                new_keys.add(key)

        for key in getattr(self, "_managed_env_keys", set()):
            if key not in new_keys:
                os.environ.pop(key, None)

        self._managed_env_keys = new_keys

    def load_added_models(self):
        """Load user-defined models for each provider from settings."""
        settings = QSettings("Ampsight", "LibreGeoLens")
        stored = settings.value("added_models", "{}")
        parsed = {}
        if isinstance(stored, str):
            try:
                parsed = json.loads(stored)
            except json.JSONDecodeError:
                parsed = {}
        elif isinstance(stored, dict):
            parsed = stored
        result = {}
        if isinstance(parsed, dict):
            for api_name, models in parsed.items():
                if isinstance(models, (list, tuple)):
                    cleaned = [m.strip() for m in models if isinstance(m, str) and m.strip()]
                    if cleaned:
                        result[api_name] = self._deduplicate_preserve_order(cleaned)
        return result

    def save_added_models(self):
        """Persist user-defined models per provider."""
        serializable = {api: self._deduplicate_preserve_order(models)
                        for api, models in self.added_models.items() if models}
        settings = QSettings("Ampsight", "LibreGeoLens")
        settings.setValue("added_models", json.dumps(serializable))

    def refresh_available_api_clients(self):
        """Populate the provider dropdown based on configured credentials."""
        self.available_api_clients = {}
        for api_name, api_info in self.supported_api_clients.items():
            if not self.get_missing_env_vars(api_info):
                self.available_api_clients[api_name] = api_info

        self.api_selection.blockSignals(True)
        self.api_selection.clear()
        if not self.available_api_clients:
            self.api_selection.addItem("No configured providers")
            self.api_selection.setEnabled(False)
            self.model_selection.clear()
            self.model_selection.setEnabled(False)
            self.api_selection.blockSignals(False)
            return

        self.api_selection.setEnabled(True)
        self.model_selection.setEnabled(True)
        for api_name in self.available_api_clients:
            self.api_selection.addItem(api_name)
        self.api_selection.blockSignals(False)
        self.update_model_choices()
        self.update_reasoning_controls_state()

    def get_missing_env_vars(self, api_config):
        missing = []
        stored_api_key = api_config.get("stored_api_key")
        stored_api_base = api_config.get("stored_api_base")
        user_env_overrides = api_config.get("user_env_overrides") or {}

        for env_info in api_config.get("env_vars", []):
            if isinstance(env_info, dict):
                name = env_info.get("name")
                required = env_info.get("required", True)
                param = env_info.get("param")
            else:
                name = env_info
                required = True
                param = None
            if not name:
                continue
            if param == "api_key" and stored_api_key:
                continue
            if param == "api_base" and stored_api_base:
                continue
            if user_env_overrides.get(name):
                continue
            if not required:
                continue
            if not os.getenv(name):
                missing.append(name)

        if missing:
            return missing

        if api_config.get("env_vars"):
            return []

        if stored_api_key:
            return []

        if api_config.get("user_env_overrides"):
            return []

        return [API_KEY_SENTINEL]

    @staticmethod
    def build_auth_params(api_config):
        params = {}
        stored_api_key = api_config.get("stored_api_key")
        if stored_api_key:
            params.setdefault("api_key", stored_api_key)

        stored_api_base = api_config.get("stored_api_base")
        if stored_api_base:
            params.setdefault("api_base", stored_api_base)

        for env_info in api_config.get("env_vars", []):
            if not isinstance(env_info, dict):
                continue
            name = env_info.get("name")
            param = env_info.get("param")
            if not name or not param:
                continue
            value = os.getenv(name)
            if not value and api_config.get("user_env_overrides"):
                value = api_config["user_env_overrides"].get(name)
            if value and param not in params:
                params[param] = value
        return params

    @staticmethod
    def _parse_env_override_value(raw_value):
        if raw_value is None or isinstance(raw_value, (bool, int, float)):
            return raw_value

        text = str(raw_value).strip()
        if text == "":
            return ""

        for parser in (json.loads, ast.literal_eval):
            try:
                return parser(text)
            except Exception:
                continue

        return raw_value

    def build_user_env_completion_kwargs(self, api_config):
        api_config = api_config or {}
        extras = {}
        user_overrides = api_config.get("user_env_overrides") or {}
        if not user_overrides:
            return extras

        defined_env_names = set()
        for env_info in api_config.get("env_vars", []):
            if isinstance(env_info, dict):
                name = env_info.get("name")
            else:
                name = env_info
            if name:
                defined_env_names.add(name)

        reserved_keys = set((api_config.get("litellm_params") or {}).keys())
        reserved_keys.update({"api_key", "api_base"})

        for key, raw_value in user_overrides.items():
            if not key:
                continue
            if key in defined_env_names:
                # Already consumed via build_auth_params / environment fallbacks
                continue
            if key in reserved_keys:
                continue
            if not any(char.islower() for char in key):
                # Treat all-uppercase style entries purely as environment variables
                continue

            parsed_value = self._parse_env_override_value(raw_value)
            extras[key] = parsed_value

        return extras

    def build_base_completion_kwargs(self, api_config):
        api_config = api_config or {}
        kwargs = dict(api_config.get("litellm_params", {}))
        kwargs.update(self.build_auth_params(api_config))
        kwargs.update(self.build_user_env_completion_kwargs(api_config))
        return kwargs

    def open_manage_services_dialog(self):
        dialog = ManageServicesDialog(
            self.iface.mainWindow(),
            self.service_templates,
            self.service_configurations,
            self.added_models,
            default_service_names=list(self.service_templates.keys()),
            deduplicate_fn=self._deduplicate_preserve_order
        )

        if dialog.exec_() == QDialog.Accepted:
            self.service_configurations = dialog.result_configurations or {}
            self.added_models = dialog.result_added_models or {}
            self.save_service_configurations()
            self.save_added_models()
            self.supported_api_clients = self.build_supported_api_clients()
            self.apply_service_env_overrides()
            self.refresh_available_api_clients()

    @staticmethod
    def _deduplicate_preserve_order(items):
        seen = set()
        result = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result

    def supports_reasoning_for_model(self, api_name, model_name):
        if not model_name:
            return False

        override = None
        if api_name:
            config = self.service_configurations.get(api_name, {})
            overrides = config.get("reasoning_overrides") if isinstance(config, dict) else None
            if isinstance(overrides, dict):
                override = overrides.get(model_name)

            if override is None:
                api_entry = getattr(self, "supported_api_clients", {}).get(api_name, {})
                entry_overrides = api_entry.get("reasoning_overrides")
                if isinstance(entry_overrides, dict):
                    override = entry_overrides.get(model_name)

        if override == "force_on":
            return True
        if override == "force_off":
            return False

        try:
            return litellm.supports_reasoning(model=model_name)
        except Exception:
            return False

    @staticmethod
    def _split_assistant_content(content):
        text_parts = []
        reasoning_parts = []

        def handle(fragment, force_reasoning=False):
            if fragment is None:
                return
            if isinstance(fragment, str):
                (reasoning_parts if force_reasoning else text_parts).append(fragment)
                return
            if isinstance(fragment, (list, tuple)):
                for item in fragment:
                    handle(item, force_reasoning=force_reasoning)
                return
            if isinstance(fragment, dict):
                fragment_type = fragment.get("type")
                type_lower = fragment_type.lower() if isinstance(fragment_type, str) else ""
                current_force_reasoning = force_reasoning or ("reason" in type_lower if type_lower else False)

                if "text" in fragment and isinstance(fragment.get("text"), str):
                    if current_force_reasoning:
                        reasoning_parts.append(fragment["text"])
                    elif not (type_lower and type_lower.startswith("tool")):
                        text_parts.append(fragment["text"])

                if "content" in fragment:
                    handle(fragment.get("content"), force_reasoning=current_force_reasoning)

                if "reasoning" in fragment:
                    handle(fragment.get("reasoning"), force_reasoning=True)

                if "delta" in fragment:
                    handle(fragment.get("delta"), force_reasoning=current_force_reasoning)
                return

            text_attr = getattr(fragment, "text", None)
            if isinstance(text_attr, str):
                (reasoning_parts if force_reasoning else text_parts).append(text_attr)

            content_attr = getattr(fragment, "content", None)
            if content_attr is not None:
                handle(content_attr, force_reasoning=force_reasoning)

            reasoning_attr = getattr(fragment, "reasoning", None)
            if reasoning_attr is not None:
                handle(reasoning_attr, force_reasoning=True)

        handle(content)
        return "".join(text_parts), "".join(reasoning_parts)

    @staticmethod
    def parse_stream_chunk(chunk):
        if not chunk:
            return "", ""
        choices = getattr(chunk, "choices", None)
        if choices is None and isinstance(chunk, dict):
            choices = chunk.get("choices")
        if not choices:
            return "", ""
        choice = choices[0]
        delta = getattr(choice, "delta", None) if not isinstance(choice, dict) else choice.get("delta")
        if delta is None:
            return "", ""

        if isinstance(delta, dict):
            content = delta.get("content")
            reasoning_payload = delta.get("reasoning_content")
        else:
            content = getattr(delta, "content", None)
            reasoning_payload = getattr(delta, "reasoning_content", None)

        text, reasoning = LibreGeoLensDockWidget._split_assistant_content(content)
        if reasoning_payload:
            extra_text, extra_reasoning = LibreGeoLensDockWidget._split_assistant_content(reasoning_payload)
            if extra_reasoning:
                reasoning += extra_reasoning
            elif extra_text:
                reasoning += extra_text
        return text, reasoning

    @staticmethod
    def parse_completion_response(response):
        if not response:
            return "", ""
        choices = getattr(response, "choices", None)
        if choices is None and isinstance(response, dict):
            choices = response.get("choices")
        if not choices:
            return "", ""
        choice = choices[0]
        message = getattr(choice, "message", None) if not isinstance(choice, dict) else choice.get("message")
        if message is None:
            return "", ""

        content = message.get("content")
        reasoning_payload = message.get("reasoning_content")

        text, reasoning = LibreGeoLensDockWidget._split_assistant_content(content)
        if reasoning_payload:
            extra_text, extra_reasoning = LibreGeoLensDockWidget._split_assistant_content(reasoning_payload)
            if extra_reasoning:
                reasoning += extra_reasoning
            elif extra_text:
                reasoning += extra_text
        return text, reasoning

    @staticmethod
    def extract_completion_text(response):
        text, _ = LibreGeoLensDockWidget.parse_completion_response(response)
        return text

    def update_model_choices(self, *_, select_model=None):
        """Update the model list based on the selected API."""
        if not getattr(self, "available_api_clients", {}):
            self.model_selection.clear()
            return
        api = self.api_selection.currentText()
        if api not in self.available_api_clients:
            self.model_selection.clear()
            return
        base_models = self.supported_api_clients.get(api, {}).get("models", [])
        added = self.added_models.get(api, [])
        combined = sorted(self._deduplicate_preserve_order(base_models + added))
        self.model_selection.clear()
        self.model_selection.addItems(combined)
        if select_model and select_model in combined:
            self.model_selection.setCurrentIndex(combined.index(select_model))
        elif combined:
            self.model_selection.setCurrentIndex(0)
        else:
            self.model_selection.setCurrentIndex(-1)

        self.update_reasoning_controls_state()

    def update_reasoning_controls_state(self):
        api_name = self.api_selection.currentText()
        model_name = self.model_selection.currentText()
        supported = self.supports_reasoning_for_model(api_name, model_name)
        override_state = None
        if api_name:
            config = self.service_configurations.get(api_name, {})
            overrides = config.get("reasoning_overrides") if isinstance(config, dict) else None
            if isinstance(overrides, dict):
                override_state = overrides.get(model_name)
            if override_state is None:
                client_entry = getattr(self, "supported_api_clients", {}).get(api_name, {})
                entry_overrides = client_entry.get("reasoning_overrides")
                if isinstance(entry_overrides, dict):
                    override_state = entry_overrides.get(model_name)

        self.reasoning_effort_combo.setEnabled(supported)
        self.reasoning_effort_label.setEnabled(supported)

        if override_state == "force_on":
            tooltip = (
                "Reasoning support is manually forced on for this model. Requests may fail if the model "
                "does not actually support reasoning."
            )
        elif override_state == "force_off":
            tooltip = "Reasoning support is manually disabled for this model."
        else:
            tooltip = "Select the amount of reasoning effort to request"

        self.reasoning_effort_combo.setToolTip(tooltip)
        self.reasoning_effort_label.setToolTip(tooltip)

    def _handle_stream_failure(self, entry, error):
        traceback_text = None
        message = "Streaming failed."
        if isinstance(error, dict):
            message_candidate = error.get("error") or error.get("message") or message
            message = str(message_candidate).strip()
            if not message:
                if isinstance(message_candidate, str) and message_candidate.strip():
                    message = message_candidate.strip()
                else:
                    message = "Streaming failed."
            traceback_text = error.get("traceback")
        elif error:
            message = str(error).strip() or "Streaming failed."

        if traceback_text:
            print(f"LibreGeoLens streaming error: {traceback_text}")

        QMessageBox.information(
            self.iface.mainWindow(),
            "Streaming Error",
            message
        )
        entry["response_stream"] = ""
        entry["reasoning_stream"] = ""
        entry["has_reasoning"] = False
        self.render_chat_history()
        QApplication.processEvents()

    def build_reasoning_params(self, model_name, api_name=None):
        api_name = api_name or self.api_selection.currentText()
        if not self.supports_reasoning_for_model(api_name, model_name):
            return {}
        params = {"reasoning_effort": self.reasoning_effort_combo.currentData()}
        return params

    def _models_for_api(self, api_name):
        base_models = self.supported_api_clients.get(api_name, {}).get("models", [])
        added_models = self.added_models.get(api_name, [])
        return self._deduplicate_preserve_order(base_models + added_models)

    def select_summary_model(self, preferred_api, preferred_model):
        available_clients = getattr(self, "available_api_clients", {})

        if not available_clients:
            api_config = self.supported_api_clients.get(preferred_api, {})
            kwargs = self.build_base_completion_kwargs(api_config)
            needs_reasoning = self.supports_reasoning_for_model(preferred_api, preferred_model)
            return preferred_api, preferred_model, kwargs, needs_reasoning

        def build_base_kwargs(api_name):
            api_config = self.supported_api_clients.get(api_name, {})
            return self.build_base_completion_kwargs(api_config)

        if preferred_api in available_clients:
            if not self.supports_reasoning_for_model(preferred_api, preferred_model):
                kwargs = build_base_kwargs(preferred_api)
                return preferred_api, preferred_model, kwargs, False

            for candidate in self._models_for_api(preferred_api):
                if not self.supports_reasoning_for_model(preferred_api, candidate):
                    kwargs = build_base_kwargs(preferred_api)
                    return preferred_api, candidate, kwargs, False

        for api_name in available_clients:
            if api_name == preferred_api:
                continue
            for candidate in self._models_for_api(api_name):
                if not self.supports_reasoning_for_model(api_name, candidate):
                    kwargs = build_base_kwargs(api_name)
                    return api_name, candidate, kwargs, False

        fallback_api = preferred_api if preferred_api in available_clients else next(iter(available_clients))
        fallback_kwargs = build_base_kwargs(fallback_api)
        fallback_models = self._models_for_api(fallback_api)
        if fallback_api == preferred_api and preferred_model:
            fallback_model = preferred_model
        elif fallback_models:
            fallback_model = fallback_models[0]
        else:
            fallback_model = preferred_model

        needs_reasoning = self.supports_reasoning_for_model(fallback_api, fallback_model)
        return fallback_api, fallback_model, fallback_kwargs, needs_reasoning

    def delete_chat(self):
        """Delete the selected chat after confirmation"""
        current_item = self.chat_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "Warning", "Please select a chat to delete.")
            return

        chat_id = current_item.data(Qt.UserRole)
        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            "Are you sure you want to delete this chat? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.Yes:

            reply = QMessageBox.question(
                self,
                "Confirm Delete Chips",
                "Do you want to delete the features & chips associated with this chat (if any)? "
                "Only the ones that haven't been used in other chats will be deleted.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )

            modified_logs = False
            # Delete chat and get chips to remove from log layer
            for image_path, chip_id in self.logs_db.delete_chat(chat_id, delete_chips=reply == QMessageBox.Yes):
                # Remove the chip's feature from log layer
                features_to_remove = []
                for feature in self.log_layer.getFeatures():
                    if str(feature["ChipId"]) == str(chip_id):
                        features_to_remove.append(feature.id())
                if features_to_remove:
                    modified_logs = True
                    self.log_layer.startEditing()
                    self.log_layer.dataProvider().deleteFeatures(features_to_remove)
                    self.log_layer.commitChanges()
                # Delete the image files
                if os.path.exists(image_path):
                    os.remove(image_path)
                raw_path = image_path.replace("_screen.png", "_raw.png")
                if os.path.exists(raw_path):
                    os.remove(raw_path)

            # Clear the chat display
            row = self.chat_list.row(current_item)
            self.chat_list.takeItem(row)
            self.current_chat_id = None
            self.conversation = []
            self.chat_history.clear()
            self.chat_list.setCurrentRow(-1)

            if modified_logs:
                # If log layer is empty, remove from disk and create from scratch instead of saving changes
                if self.log_layer.featureCount() == 0:
                    os.remove(os.path.join(self.logs_dir, "logs.geojson"))
                    self.handle_log_layer()
                else:
                    # Save changes to geojson
                    self.save_logs_to_geojson()
                    self.handle_log_layer()

            # Start new chat if no chats left
            if self.chat_list.count() == 0:
                self.start_new_chat()

    def save_image_to_logs(self, image, chip_id, raw=False):
        image_dir = os.path.join(self.logs_dir, "chips")
        os.makedirs(image_dir, exist_ok=True)
        # Save the image file in the created directory
        image_path = os.path.join(image_dir, f"{chip_id}_screen.png")
        if raw:
            image.save(image_path.replace("_screen.png", "_raw.png"), "PNG")
        else:
            image.save(image_path, "PNG")
        return image_path

    def send_to_mllm_fn(self):
        try:
            self.send_to_mllm()
        except Exception as e:
            QMessageBox.warning(self.iface.mainWindow(), "Error", str(e))
            self.reload_current_chat()

    def send_to_mllm(self):
        if self.current_chat_id is None:
            QMessageBox.warning(self, "Error", "Please select a chat or start a new chat before prompting.")
            return

        selected_api = self.api_selection.currentText()
        if selected_api not in getattr(self, "available_api_clients", {}):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Error",
                "No configured MLLM service found. Use Manage Services to configure credentials, then try again."
            )
            return

        selected_model = self.model_selection.currentText()
        if not selected_model:
            QMessageBox.warning(self, "Error", "No model available for the selected MLLM service.")
            return

        api_config = self.supported_api_clients[selected_api]
        missing_env = self.get_missing_env_vars(api_config)
        if missing_env:
            if missing_env == [API_KEY_SENTINEL]:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Error",
                    f"{selected_api} configuration incomplete. Add an API key in Manage Services or expose one via environment variables."
                )
            else:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Error",
                    f"{selected_api} configuration incomplete. Missing environment variables: {', '.join(missing_env)}."
                )
            return

        base_completion_kwargs = self.build_base_completion_kwargs(api_config)
        reasoning_params = self.build_reasoning_params(selected_model, selected_api)
        base_completion_kwargs.update(reasoning_params)

        prompt = self.prompt_input.toPlainText()
        if not prompt.strip():
            QMessageBox.warning(self, "Error", "Please enter a prompt.")
            return

        self.prompt_input.clear()

        stop_prompt = "I have stopped your response"
        if self.conversation and self.conversation[-1].get("role") == "user":
            last_content = self.conversation[-1].get("content") or []
            first_chunk = last_content[0] if last_content else None
            last_text = ""
            if isinstance(first_chunk, dict) and first_chunk.get("type") == "text":
                last_text = first_chunk.get("text", "")
            if last_text and last_text.startswith(stop_prompt):
                prompt = self.conversation[-1]["content"][0]["text"] + ". " + prompt
                self.conversation[-1]["content"][0]["text"] = prompt
        else:
            user_message = {"role": "user", "content": [{"type": "text", "text": prompt}]}
            self.conversation.append(user_message)

        n_images = len(self.image_display_widget.images)
        chip_ids_sequence, chip_modes_sequence = [], []
        chips_original_resolutions, chips_actual_resolutions = [], []
        chips_display = []
        send_raw = self.radio_raw.isChecked()

        for idx in range(n_images):
            image_entry = self.image_display_widget.images[idx]
            image_path = image_entry["image_path"]
            image_to_send = image_entry["image"]

            if image_path is None:
                rectangle_geom = image_entry["rectangle_geom"]
                polygon_coords = rectangle_geom.asPolygon()
                chip_id = self.logs_db.save_chip(
                    image_path="tmp_image_path.png",
                    geocoords=[[point.x(), point.y()] for point in polygon_coords[0]] +
                              [[polygon_coords[0][0].x(), polygon_coords[0][0].y()]]
                )
                chip_ids_sequence.append(chip_id)
                image_path = self.save_image_to_logs(image_to_send, chip_id)
                self.image_display_widget.images[idx]["image_path"] = image_path
                self.logs_db.update_chip_image_path(chip_id, image_path)
            else:
                chip_id = int(ntpath.basename(image_path).split(".")[0].split("_screen")[0])
                chip_ids_sequence.append(chip_id)

            chip_mode = "raw" if send_raw else "screen"
            chip_modes_sequence.append(chip_mode)

            if chip_mode == "raw":
                raw_image_path = image_path.replace("_screen.png", "_raw.png")

                if not os.path.exists(raw_image_path):
                    rectangle = image_entry["rectangle_geom"].boundingBox()
                    cog_path = ru.find_topmost_cog_feature(rectangle)
                    if cog_path is None:
                        QMessageBox.information(
                            self.iface.mainWindow(),
                            "No Overlapping COG",
                            "No raw imagery layer containing the drawn area could be found."
                        )
                        self.reload_current_chat()
                        return

                    drawn_box_geocoords = ru.get_drawn_box_geocoordinates(rectangle, cog_path)
                    chip_width, chip_height = ru.determine_chip_size(drawn_box_geocoords, cog_path)

                    if max(chip_width, chip_height) > 2048:
                        reply = QMessageBox.question(
                            self,
                            "Confirm Chip",
                            f"The raw chip to be extracted will be {chip_width}x{chip_height}. "
                            f"Depending on your machine, this might be too intensive. "
                            f"The chip might also be downscaled to comply with the MLLM service limits "
                            f"(see the i button next to the Send Raw Chip radio button). Do you still want to proceed?",
                            QMessageBox.Yes | QMessageBox.No,
                            QMessageBox.No
                        )
                        if reply == QMessageBox.No:
                            self.reload_current_chat()
                            return

                    center_latitude = (drawn_box_geocoords.yMinimum() + drawn_box_geocoords.yMaximum()) / 2
                    center_longitude = (drawn_box_geocoords.xMinimum() + drawn_box_geocoords.xMaximum()) / 2

                    image_to_send = ru.extract_chip_from_tif_point_in_memory(
                        img_path=cog_path,
                        center_latitude=center_latitude,
                        center_longitude=center_longitude,
                        chip_width_px=chip_width,
                        chip_height_px=chip_height
                    )
                    self.save_image_to_logs(image_to_send, chip_ids_sequence[-1], raw=True)

                image_base64, dimensions = self.load_image_base64_downscale_if_needed(raw_image_path, selected_api)
                chips_original_resolutions.append(dimensions["original"])
                chips_actual_resolutions.append(dimensions["final"])
                user_message["content"].append(
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}}
                )
            else:
                image_base64, dimensions = self.load_image_base64_downscale_if_needed(image_path, selected_api)
                chips_original_resolutions.append(dimensions["original"])
                chips_actual_resolutions.append(dimensions["final"])
                user_message["content"].append(
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}}
                )

            normalized_path = self._normalize_path(image_path)
            resolution_text = self._format_resolution_text(dimensions["original"], dimensions["final"])
            chips_display.append({
                "image_path": normalized_path,
                "mode_label": "Raw" if chip_mode == "raw" else "Screen",
                "resolution_text": resolution_text,
            })

        entry = {
            "display_id": str(uuid.uuid4()),
            "db_id": None,
            "prompt": prompt,
            "chips": chips_display,
            "mllm_service": selected_api,
            "mllm_model": selected_model,
            "response": "",
            "response_stream": "",
            "reasoning": None,
            "reasoning_stream": "",
            "reasoning_visible": False,
            "is_pending": True,
            "has_reasoning": False,
        }
        self.rendered_interactions.append(entry)
        self.chat_auto_scroll_enabled = True
        self.render_chat_history()
        QApplication.processEvents()

        processed_conversation = []
        for message in self.conversation:
            processed_message = {"role": message["role"]}

            if message["role"] == "assistant":
                processed_message["content"] = message["content"]
                processed_conversation.append(processed_message)
                continue

            processed_content = []
            for content in message["content"]:
                if content.get("type") == "local_image_path":
                    image_path = content["path"]
                    if os.path.exists(image_path):
                        image_base64, _ = self.load_image_base64_downscale_if_needed(image_path, selected_api)
                        processed_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_base64}"}
                        })
                else:
                    processed_content.append(content)

            processed_message["content"] = processed_content
            processed_conversation.append(processed_message)

        stream_supported = api_config.get("supports_streaming", True)
        entry["response_stream"] = ""
        entry["reasoning_stream"] = ""

        entry_id = entry["display_id"]
        request_context = {
            "prompt": prompt,
            "selected_api": selected_api,
            "selected_model": selected_model,
            "chip_ids_sequence": chip_ids_sequence,
            "chip_modes_sequence": chip_modes_sequence,
            "chips_original_resolutions": chips_original_resolutions,
            "chips_actual_resolutions": chips_actual_resolutions,
            "n_images": n_images,
            "user_message_index": len(self.conversation) - 1,
        }

        worker = MLLMStreamWorker(
            selected_model,
            processed_conversation,
            base_completion_kwargs,
            stream_supported,
            self.parse_stream_chunk,
            self.parse_completion_response
        )
        thread = QThread()
        worker.moveToThread(thread)

        request_context["worker"] = worker
        request_context["thread"] = thread
        self.active_streams[entry_id] = request_context
        self._lock_ui_for_stream()
        self.cancel_mllm_button.setEnabled(True)

        worker.chunk_received.connect(self._worker_chunk_received)
        worker.stream_failed.connect(self._worker_stream_failed)
        worker.completed.connect(self._worker_completed)
        worker.failed.connect(self._worker_failed)
        worker.done.connect(thread.quit)
        worker.done.connect(self._worker_done)

        thread.started.connect(worker.run)
        thread.start()

    def _disconnect_worker_stream_signals(self, worker):
        # Keep 'done' so cleanup proceeds; silence all others
        try:
            worker.chunk_received.disconnect(self._worker_chunk_received)
        except Exception:
            pass
        try:
            worker.stream_failed.disconnect(self._worker_stream_failed)
        except Exception:
            pass
        try:
            worker.completed.disconnect(self._worker_completed)
        except Exception:
            pass
        try:
            worker.failed.disconnect(self._worker_failed)
        except Exception:
            pass

    def cancel_active_mllm_request(self):
        if not self.active_streams:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Nothing to Cancel",
                "There is no Send to MLLM request in progress."
            )
            return

        pending_entry = None
        entry_id = None
        for candidate in reversed(self.rendered_interactions):
            candidate_id = candidate.get("display_id")
            if not candidate.get("is_pending"):
                continue
            if candidate_id in self.active_streams:
                pending_entry = candidate
                entry_id = candidate_id
                break

        if entry_id is None:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Nothing to Cancel",
                "There is no Send to MLLM request in progress."
            )
            return

        context = self.active_streams.get(entry_id)
        if context is None:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Nothing to Cancel",
                "There is no Send to MLLM request in progress."
            )
            return

        worker = context.get("worker")
        if worker is not None:
            # Thread-safe cancellation into the worker's thread:
            QMetaObject.invokeMethod(worker, "request_cancel", Qt.QueuedConnection)
            # Prevent any other UI-updating signals from the cancelled worker:
            self._disconnect_worker_stream_signals(worker)

        has_stream_output = bool(
            (pending_entry.get("response_stream") or "")
            or (pending_entry.get("reasoning_stream") or "")
        )

        if has_stream_output:
            context["handled_cancel"] = True
            response_text = pending_entry.get("response_stream") or ""
            reasoning_text = pending_entry.get("reasoning_stream") or ""
            self._finalize_interaction(
                entry_id,
                pending_entry,
                context,
                response_text,
                reasoning_text,
                skip_reload=True
            )

            stop_prompt = "I have stopped your response"
            self.conversation.append({
                "role": "user",
                "content": [{"type": "text", "text": stop_prompt}]
            })

            self.chat_auto_scroll_enabled = True

        else:
            context["handled_cancel"] = True
            prompt_text = context.get("prompt", "")
            user_idx = context.get("user_message_index")
            if user_idx is not None and 0 <= user_idx < len(self.conversation):
                self.conversation = self.conversation[:user_idx]

            if pending_entry in self.rendered_interactions:
                self.rendered_interactions.remove(pending_entry)

            if prompt_text:
                self.prompt_input.setPlainText(prompt_text)

            self.chat_auto_scroll_enabled = True
            self.render_chat_history()

            context["ready_for_cleanup"] = True
            if all(ctx.get("ready_for_cleanup") for ctx in self.active_streams.values()):
                self._unlock_ui_after_stream()

        self.cancel_mllm_button.setEnabled(False)

    def _get_rendered_entry(self, entry_id):
        for entry in self.rendered_interactions:
            if entry.get("display_id") == entry_id:
                return entry
        return None

    def _handle_stream_chunk(self, entry_id, text_chunk, reasoning_chunk):
        entry = self._get_rendered_entry(entry_id)
        if not entry:
            return
        if not entry.get("is_pending"):
            return
        updated = False
        if text_chunk:
            entry["response_stream"] += text_chunk
            updated = True
        if reasoning_chunk:
            entry["reasoning_stream"] += reasoning_chunk
            entry["has_reasoning"] = True
            updated = True
        if updated:
            self.render_chat_history()

    def _handle_unexpected_exception(self, error, context="", traceback_text=None, *, show_dialog=True):
        message = str(error) if error else "An unexpected error occurred."
        title = "LibreGeoLens Error"
        if context:
            title = f"{title} ({context})"

        details = traceback_text or traceback.format_exc()
        if details:
            if context:
                print(f"LibreGeoLens error in {context}:\n{details}")
            else:
                print(f"LibreGeoLens error:\n{details}")

        if show_dialog:
            QMessageBox.critical(self.iface.mainWindow(), title, message)

    def _find_entry_id_for_worker(self, worker):
        for entry_id, ctx in self.active_streams.items():
            if ctx.get("worker") is worker:
                return entry_id
        return None

    def _dispatch_worker_callback(self, callback, entry_id, *args, propagate_error=True):
        try:
            callback(entry_id, *args)
        except Exception as exc:
            callback_name = getattr(callback, "__name__", repr(callback))
            traceback_text = traceback.format_exc()
            self._handle_unexpected_exception(
                exc,
                context=callback_name,
                traceback_text=traceback_text,
                show_dialog=not propagate_error,
            )

            if not propagate_error or entry_id is None:
                return

            payload = {
                "final_error": exc,
                "traceback": traceback_text,
            }
            try:
                self._on_stream_error(entry_id, payload)
            except Exception as inner_exc:
                inner_trace = traceback.format_exc()
                self._handle_unexpected_exception(
                    inner_exc,
                    context="_on_stream_error",
                    traceback_text=inner_trace,
                )

    @pyqtSlot(object, object)
    def _worker_chunk_received(self, text_chunk, reasoning_chunk):
        worker = self.sender()
        entry_id = self._find_entry_id_for_worker(worker)
        if entry_id is None:
            return
        self._dispatch_worker_callback(
            self._handle_stream_chunk,
            entry_id,
            text_chunk,
            reasoning_chunk,
        )

    @pyqtSlot(object)
    def _worker_stream_failed(self, error):
        worker = self.sender()
        entry_id = self._find_entry_id_for_worker(worker)
        if entry_id is None:
            return
        entry = self._get_rendered_entry(entry_id)
        if not entry:
            return
        try:
            self._handle_stream_failure(entry, error)
        except Exception as exc:
            traceback_text = traceback.format_exc()
            self._handle_unexpected_exception(exc, context="_handle_stream_failure", traceback_text=traceback_text)

    @pyqtSlot(object)
    def _worker_completed(self, payload):
        worker = self.sender()
        entry_id = self._find_entry_id_for_worker(worker)
        if entry_id is None:
            return
        self._dispatch_worker_callback(
            self._on_stream_completed,
            entry_id,
            payload,
        )

    @pyqtSlot(object)
    def _worker_failed(self, payload):
        worker = self.sender()
        entry_id = self._find_entry_id_for_worker(worker)
        if entry_id is None:
            return
        self._dispatch_worker_callback(
            self._on_stream_error,
            entry_id,
            payload,
            propagate_error=False,
        )

    @pyqtSlot()
    def _worker_done(self):
        worker = self.sender()
        entry_id = self._find_entry_id_for_worker(worker)
        if entry_id is None:
            return
        self._dispatch_worker_callback(
            self._on_stream_done,
            entry_id,
            propagate_error=False,
        )

    def _on_stream_failed(self, entry_id, error):
        entry = self._get_rendered_entry(entry_id)
        if entry:
            self._handle_stream_failure(entry, error)

    def _on_stream_completed(self, entry_id, payload):
        entry = self._get_rendered_entry(entry_id)
        context = self.active_streams.get(entry_id)
        if not entry or context is None:
            return

        if context.get("handled_cancel"):
            return

        stream_success = bool(payload.get("stream_success"))
        response_text = payload.get("response_text") or ""
        reasoning_text = payload.get("reasoning_text") or ""

        if stream_success:
            response_text = entry.get("response_stream", "") or response_text
            reasoning_text = entry.get("reasoning_stream", "") or reasoning_text
        else:
            entry["response_stream"] = response_text
            entry["reasoning_stream"] = reasoning_text
            entry["has_reasoning"] = bool(reasoning_text)

        self._finalize_interaction(entry_id, entry, context, response_text, reasoning_text)

    def _on_stream_error(self, entry_id, error_payload):
        context = self.active_streams.get(entry_id)
        final_error = None
        stream_error = None
        unexpected_error = None
        traceback_text = None

        if isinstance(error_payload, dict):
            final_error = error_payload.get("final_error")
            stream_error = error_payload.get("stream_error")
            unexpected_error = error_payload.get("unexpected_error")
            traceback_text = error_payload.get("traceback")
        elif isinstance(error_payload, Exception):
            final_error = error_payload
        entry = self._get_rendered_entry(entry_id)

        if context and context.get("handled_cancel"):
            return

        if context:
            user_idx = context.get("user_message_index")
            if user_idx is not None and 0 <= user_idx < len(self.conversation):
                self.conversation = self.conversation[:user_idx]

        if entry and entry in self.rendered_interactions:
            self.rendered_interactions.remove(entry)

        self.render_chat_history()

        message = (
            str(final_error)
            or str(unexpected_error)
            or str(stream_error)
            or "An error occurred while completing the request."
        )

        if traceback_text:
            print(f"LibreGeoLens interaction error: {traceback_text}")

        QMessageBox.warning(self.iface.mainWindow(), "Error", message)
        if context is not None:
            context["handled_error"] = True
        self.reload_current_chat()

    def _on_stream_done(self, entry_id):
        context = self.active_streams.get(entry_id)
        if not context:
            return

        worker = context.get("worker")
        thread = context.get("thread")

        # 1) Make sure no more UI-updating signals can land
        if worker:
            self._disconnect_worker_stream_signals(worker)

        # 2) Ensure the thread is stopped
        if thread and thread.isRunning():
            thread.quit()
            # Be gentle; don’t hang the UI here if a provider misbehaves
            thread.wait(100)

        # 3) Now it’s safe to delete the objects
        if worker:
            worker.deleteLater()
        if thread:
            thread.deleteLater()

        # 4) Drop references and finish normal cleanup
        context.pop("worker", None)
        context.pop("thread", None)
        self._cleanup_active_stream(entry_id, context)

    def _finalize_interaction(self, entry_id, entry, context, response_text, reasoning_text, skip_reload=False):
        entry["response"] = response_text or ""
        entry["reasoning"] = reasoning_text if reasoning_text else None
        entry["response_stream"] = ""
        entry["reasoning_stream"] = ""
        entry["is_pending"] = False
        entry["has_reasoning"] = bool(reasoning_text)
        self.render_chat_history()

        response = entry["response"]
        reasoning_to_persist = entry["reasoning"]
        self.conversation.append({"role": "assistant", "content": response})

        interaction_id = self.logs_db.save_interaction(
            text_input=context["prompt"],
            text_output=response,
            chips_sequence=context["chip_ids_sequence"],
            mllm_service=context["selected_api"],
            mllm_model=context["selected_model"],
            chips_mode_sequence=context["chip_modes_sequence"],
            chips_original_resolutions=context["chips_original_resolutions"],
            chips_actual_resolutions=context["chips_actual_resolutions"],
            reasoning_output=reasoning_to_persist
        )
        entry["db_id"] = interaction_id
        self.logs_db.add_new_interaction_to_chat(self.current_chat_id, interaction_id)

        summary_text = ""
        summary_api = context["selected_api"]
        summary_model = context["selected_model"]
        try:
            summary_api, summary_model, summary_kwargs, needs_reasoning = self.select_summary_model(
                summary_api, summary_model
            )
            if needs_reasoning:
                summary_kwargs["reasoning_effort"] = "low"
            summary_response = litellm.completion(
                model=summary_model,
                messages=[
                    {"role": "user", "content": [{"type": "text", "text":
                        f"Summarize the following in 10 words or less: {self.chat_history.toPlainText()}."
                        f" Only respond with your summary."}]}
                ],
                **summary_kwargs,
                allowed_openai_params=['reasoning_effort'] if 'reasoning_effort' in summary_kwargs else []
            )
            summary_text = self.extract_completion_text(summary_response).strip()
        except Exception as exc:
            print(f"Failed to generate summary via {summary_api} ({summary_model}): {exc}")

        if summary_text:
            self.logs_db.update_chat_summary(self.current_chat_id, summary_text)
            current_item = self.chat_list.currentItem()
            if current_item is not None:
                current_item.setText(summary_text)

        n_images = context["n_images"]
        for idx in range(n_images):
            if idx >= len(self.image_display_widget.images):
                break
            image_metadata = self.image_display_widget.images[idx]
            request = QgsFeatureRequest().setFilterExpression(
                f'"ChipId" = \'{image_metadata.get("chip_id", "")}\''
            )
            for feature in self.log_layer.getFeatures(request):
                feat_attrs = feature.attributes()
                interactions = feat_attrs[0]
                if isinstance(interactions, str):
                    interactions = json.loads(interactions)
                interaction_payload = {"prompt": context["prompt"], "response": response}
                if reasoning_to_persist:
                    interaction_payload["reasoning"] = reasoning_to_persist

                if len(interactions) > 0:
                    interactions[interaction_id] = interaction_payload
                    self.log_layer.dataProvider().changeAttributeValues({
                        feature.id(): {0: json.dumps(interactions)}
                    })
                else:
                    interactions[interaction_id] = interaction_payload
                    image_metadata["chip_id"] = context["chip_ids_sequence"][idx]
                    self.log_layer.dataProvider().changeAttributeValues({
                        feature.id(): {
                            0: json.dumps(interactions),
                            1: image_metadata.get("image_path"),
                            2: image_metadata.get("chip_id")
                        }
                    })
                break

        if n_images > 0:
            self.log_layer.updateExtents()
            self.save_logs_to_geojson()
            self.handle_log_layer()
            if self.area_drawing_tool:
                self.area_drawing_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.image_display_widget.clear_images()

        QTimer.singleShot(0, lambda: SettingsDialog(self.iface.mainWindow())
                          .sync_local_logs_dir_with_s3(self.logs_dir))

        context["ready_for_cleanup"] = True

        if all(ctx.get("ready_for_cleanup") for ctx in self.active_streams.values()):
            self.cancel_mllm_button.setEnabled(False)
            self._unlock_ui_after_stream()

        if not skip_reload:
            QTimer.singleShot(0, self.reload_current_chat)

    def _cleanup_active_stream(self, entry_id, context=None):
        ctx = context if context is not None else self.active_streams.get(entry_id)
        self.active_streams.pop(entry_id, None)
        if not ctx:
            if not self.active_streams:
                if hasattr(self, "cancel_mllm_button"):
                    self.cancel_mllm_button.setEnabled(False)
                self._unlock_ui_after_stream()
            return
        worker = ctx.pop("worker", None)
        thread = ctx.pop("thread", None)
        if thread and thread.isRunning():
            thread.quit()
        if not self.active_streams:
            if hasattr(self, "cancel_mllm_button"):
                self.cancel_mllm_button.setEnabled(False)
            self._unlock_ui_after_stream()

    def reload_current_chat(self):
        item = self.chat_list.currentItem()
        self.chat_list.setCurrentItem(item)
        self.load_chat(item)
        scrollbar = self.chat_history.verticalScrollBar()
        if scrollbar is not None:
            with QSignalBlocker(scrollbar):
                scrollbar.setValue(scrollbar.maximum())

    def show_quick_help(self, first_time=False):
        """Display a quick help guide with workflow steps in a non-modal dialog"""
        help_text = """
        <h2>LibreGeoLens Quick Guide</h2>
        
        Recommended: Read GitHub repo's <a href="https://github.com/ampsight/LibreGeoLens/tree/main?
        tab=readme-ov-file#quickstart">Quickstart</a> and <a href="https://github.com/ampsight/LibreGeoLens/
        tree/main?tab=readme-ov-file#more-features">More Features</a> sections
        
        <h3>Basic Workflow:</h3>
        <ol>
            <li><b>Load basemap layer</b>:
                <ul>
                    <li>Load a basemap layer</li>
                    <li>This is optional but very helpful, specially when working with GeoJSON COG outlines (see below)</li>
                </ul>
            </li>
            <li><b>Load imagery</b>:
                <ul>
                    <li>Open your local georeferenced imagery directly with QGIS or click
                     <b>Load GeoJSON</b> to load COG image outlines (red polygons)</li>
                    <li>Choose <b>Use Demo Resources</b> if you don't have your own data</li>
                    <li>If you want to use your own COGs, refer to the GitHub repo on how to do that</li>
                    <li>If you used <b>Load GeoJSON</b>, zoom into one of the red polygons, click <b>Draw Area
                     to Stream COGs</b>, and draw a rectangle over the red polygon to load the imagery</li>
                </ul>
            </li>
            <li><b>Extract an image chip</b>:
                <ul>
                    <li>Zoom to an area of interest</li>
                    <li>Click <b>Draw Area to Chip Imagery</b> and draw a rectangle to extract that area</li>
                    <li>The extracted chip will appear in the image area above the "Send to MLLM" button</li>
                </ul>
            </li>
            <li><b>Ask about the imagery</b>:
                <ul>
                    <li>Choose whether to send a <b>Screen Chip</b> (what you see) or <b>Raw Chip</b> (original data)</li>
                    <li>Type your question in the prompt box</li>
                    <li>Select the MLLM service and model</li>
                    <li>Click <b>Send to MLLM</b> to get a response about your image</li>
                </ul>
            </li>
            <li><b>Interact with results</b>:
                <ul>
                    <li>Click on an image in the chat to select it and highlight its location on the map</li>
                    <li>Click <b>Select Area</b> then click on an orange rectangle to see where it was used in chats</li>
                    <li>Double-click on image chips to view them at full size</li>
                </ul>
            </li>
        </ol>
        <h3>Tips:</h3>
        <ul>
            <li>Hover over buttons and UI elements to see tooltips explaining their functions</li>
            <li>You need API keys configured in QGIS environment settings (see <i>icon</i> → Settings)</li>
            <li>For large areas, raw chip extraction can be resource intensive</li>
            <li>All chips are saved as GeoJSON features (orange rectangles) for easy reference</li>
            <li>Click the "i" button by the radio buttons for info about image size limits</li>
            <li>If LiteLLM mislabels a reasoning-capable model, open <b>Manage MLLM Services</b> and use the
            <b>Reasoning Override</b> control. Only force reasoning when you're sure—the provider may fail if the
            model isn't built for reasoning.</li>
        </ul>
        """

        # Check if we already have an open help dialog
        if self.help_dialog is not None:
            # If dialog exists, just make sure it's visible and bring to front
            self.help_dialog.show()
            self.help_dialog.raise_()
            self.help_dialog.activateWindow()
            return

        # Create a new dialog
        self.help_dialog = QDialog(self)
        self.help_dialog.setWindowTitle("LibreGeoLens Help")
        self.help_dialog.resize(600, 700)  # Set a reasonable size

        # If this is the first-time display (on plugin startup), make it stay on top
        if first_time:
            self.help_dialog.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)

        # Create layout
        layout = QVBoxLayout()

        # Create text browser for rich text display
        text_browser = QTextBrowser()
        text_browser.setOpenExternalLinks(True)  # Allow opening links
        text_browser.setHtml(help_text)
        layout.addWidget(text_browser)

        # Add a close button
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.help_dialog.close)
        layout.addWidget(close_button)

        self.help_dialog.setLayout(layout)

        # Handle dialog closure to reset the reference
        self.help_dialog.finished.connect(self.on_help_dialog_closed)

        # Show the dialog non-modally
        self.help_dialog.show()

    def on_help_dialog_closed(self):
        """Reset the help_dialog reference when the dialog is closed"""
        self.help_dialog = None

    def export_chat(self):
        """Export the current chat as a self-contained HTML file and GeoJSON"""
        if self.current_chat_id is None:
            QMessageBox.warning(self, "Error", "Please select a chat to export.")
            return

        # Get chat data
        chat = self.logs_db.fetch_chat_by_id(self.current_chat_id)
        chat_summary = chat[2]
        interactions_sequence = json.loads(chat[1])

        # Create a timestamp for unique folder name
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_summary = ''.join(c if c.isalnum() else '_' for c in chat_summary)[:30]  # First 30 chars, alphanumeric only
        export_folder_name = f"chat_{self.current_chat_id}_{safe_summary}_{timestamp}"
        export_folder_path = os.path.join(self.logs_dir, "exports", export_folder_name)
        os.makedirs(export_folder_path, exist_ok=True)
        
        # Create images folder inside export folder
        images_folder_path = os.path.join(export_folder_path, "images")
        os.makedirs(images_folder_path, exist_ok=True)
        
        # Collect all chips used in this chat
        all_chip_ids = []
        for interaction_id in interactions_sequence:
            (_, _, _, chip_ids, _, _, chip_modes, _, _, _) = self.logs_db.fetch_interaction_by_id(interaction_id)
            all_chip_ids.extend(json.loads(chip_ids))
        
        # Copy all chip images to the export folder
        chip_path_mapping = {}  # Original path -> exported path mapping
        for chip_id in all_chip_ids:
            chip = self.logs_db.fetch_chip_by_id(chip_id)
            if chip:
                original_path = chip[1]
                filename = os.path.basename(original_path)
                exported_path = os.path.join(images_folder_path, filename)
                
                # Copy image file if it exists
                if os.path.exists(original_path):
                    shutil.copy2(original_path, exported_path)
                    chip_path_mapping[original_path] = os.path.join("images", filename)
                    
                    # Check for raw version
                    raw_path = original_path.replace("_screen.png", "_raw.png")
                    if os.path.exists(raw_path):
                        raw_filename = os.path.basename(raw_path)
                        exported_raw_path = os.path.join(images_folder_path, raw_filename)
                        shutil.copy2(raw_path, exported_raw_path)
                        chip_path_mapping[raw_path] = os.path.join("images", raw_filename)
        
        # Generate HTML for the chat
        html_content = self._generate_chat_html(interactions_sequence, chip_path_mapping)
        
        # Write HTML file
        html_path = os.path.join(export_folder_path, "chat.html")
        with open(html_path, 'w', encoding='utf-8') as html_file:
            html_file.write(html_content)
        
        # Export GeoJSON features related to this chat
        self._export_chat_geojson(export_folder_path, all_chip_ids)

        self.open_directory(export_folder_path)
        
    def _generate_chat_html(self, interactions_sequence, chip_path_mapping):
        """Generate a self-contained HTML representation of the chat"""
        # HTML header with styling
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LibreGeoLens Chat Export</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            line-height: 1.6;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .header {
            text-align: center;
            margin-bottom: 30px;
        }
        .header h1 {
            color: #333;
        }
        .chat-container {
            background-color: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .user-message, .assistant-message {
            margin-bottom: 15px;
            padding: 10px 15px;
            border-radius: 8px;
        }
        .user-message {
            background-color: #e6f7ff;
            border-left: 4px solid #1890ff;
        }
        .assistant-message {
            background-color: #f6ffed;
            border-left: 4px solid #52c41a;
        }
        .chip-container {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin: 10px 0;
        }
        .chip {
            position: relative;
            display: inline-block;
            margin-bottom: 10px;
        }
        .chip img {
            max-width: 300px;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        .chip-label {
            position: absolute;
            top: 3px;
            right: 5px;
            background: rgba(0,0,0,0.5);
            color: white;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 12px;
        }
        .resolution-label {
            position: absolute;
            bottom: 3px;
            left: 5px;
            background: rgba(0,0,0,0.5);
            color: white;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 12px;
        }
        details.reasoning-block {
            margin-bottom: 15px;
        }
        details.reasoning-block summary {
            font-weight: 600;
            cursor: pointer;
        }
        details.reasoning-block[open] summary {
            margin-bottom: 6px;
        }
        .reasoning-body {
            background-color: #f9f9f9;
            border-left: 3px solid #aaa;
            padding: 8px 12px;
        }
        .footer {
            text-align: center;
            margin-top: 30px;
            font-size: 12px;
            color: #888;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>LibreGeoLens Chat Export</h1>
        <p>Exported on: """ + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + """</p>
    </div>
    <div class="chat-container">
"""
        
        # Add each interaction to the HTML
        for interaction_id in interactions_sequence:
            (_, prompt, response, chip_ids, mllm_service, mllm_model, chip_modes,
             original_resolutions, actual_resolutions, reasoning_output) = self.logs_db.fetch_interaction_by_id(interaction_id)
            
            # User message
            html += f'<div class="user-message">\n<strong>User:</strong> {prompt}\n</div>\n'
            
            # Process chips
            chip_ids_list = json.loads(chip_ids)
            chip_modes_list = ast.literal_eval(chip_modes)
            
            try:
                original_resolutions_list = ast.literal_eval(original_resolutions) if original_resolutions else []
                actual_resolutions_list = ast.literal_eval(actual_resolutions) if actual_resolutions else []
            except (TypeError, json.JSONDecodeError):
                original_resolutions_list = []
                actual_resolutions_list = []
            
            # Ensure lists exist and have correct length (for backward compatibility)
            if not original_resolutions_list:
                original_resolutions_list = ["Unknown"] * len(chip_ids_list)
            if not actual_resolutions_list:
                actual_resolutions_list = ["Unknown"] * len(chip_ids_list)
            
            # Add chip images to HTML
            if chip_ids_list:
                html += '<div class="chip-container">\n'
                
                for i, (chip_id, chip_mode) in enumerate(zip(chip_ids_list, chip_modes_list)):
                    image_path = self.logs_db.fetch_chip_by_id(chip_id)[1]
                    
                    # Get mapped path (images are copied to images folder)
                    if image_path in chip_path_mapping:
                        relative_path = chip_path_mapping[image_path]
                        
                        # Get resolution information
                        original_res = original_resolutions_list[i] if i < len(original_resolutions_list) else "Unknown"
                        actual_res = actual_resolutions_list[i] if i < len(actual_resolutions_list) else "Unknown"
                        
                        # Create resolution display text
                        resolution_text = ""
                        if original_res != "Unknown":
                            if original_res != actual_res:
                                resolution_text = f"{original_res} → {actual_res}"
                            else:
                                resolution_text = f"{original_res}"
                        
                        html += f'''<div class="chip">
    <img src="{relative_path}" alt="Image chip">
    <span class="chip-label">{"Raw" if chip_mode == "raw" else "Screen"} Chip</span>
    <span class="resolution-label">{resolution_text}</span>
</div>\n'''
                
                html += '</div>\n'
            
            # Assistant response
            html += (
                f'<div class="assistant-message">\n'
                f'<strong>{mllm_model} ({mllm_service}):</strong> {markdown.markdown(response)}\n'
                f'</div>\n'
            )

            normalized_reasoning = reasoning_output if reasoning_output not in (None, "", "None") else None
            if normalized_reasoning:
                reasoning_body = markdown.markdown(normalized_reasoning)
            else:
                reasoning_body = "<p><em>Reasoning content not available.</em></p>"

            html += (
                '<details class="reasoning-block">\n'
                '    <summary>Reasoning</summary>\n'
                f'    <div class="reasoning-body">{reasoning_body}</div>\n'
                '</details>\n'
            )
        
        # HTML footer
        html += """    </div>
    <div class="footer">
        <p>Generated by LibreGeoLens - A QGIS plugin for experimenting with Multimodal Large Language Models to analyze remote sensing imagery</p>
    </div>
</body>
</html>"""
        
        return html
    
    def _export_chat_geojson(self, export_folder_path, chip_ids):
        """Export GeoJSON features related to this chat"""
        # Get chat data to know which interactions belong to this chat
        chat = self.logs_db.fetch_chat_by_id(self.current_chat_id)
        interactions_sequence = json.loads(chat[1])
        chat_interaction_ids = [str(id) for id in interactions_sequence]  # Convert to strings for comparison
        
        # Create a new GeoJSON object with just the features associated with chip_ids
        geojson_features = []
        
        # Collect features from log layer associated with this chat
        for feature in self.log_layer.getFeatures():
            feature_chip_id = feature["ChipId"]
            if feature_chip_id is not None and int(feature_chip_id) in chip_ids:
                # Convert QGIS feature to GeoJSON feature
                geometry = feature.geometry()
                if geometry:
                    geojson_geometry = json.loads(geometry.asJson())
                    
                    # Convert attributes to properties
                    properties = {}
                    for field in self.log_layer.fields():
                        field_name = field.name()
                        field_value = feature[field_name]
                        
                        # Filter interactions to only include those from this chat
                        if field_name == "Interactions":
                            if field_value and isinstance(field_value, str):
                                try:
                                    interactions_dict = json.loads(field_value)
                                    # Only keep interactions that belong to this chat
                                    filtered_interactions = {}
                                    for interaction_id, interaction_data in interactions_dict.items():
                                        if interaction_id in chat_interaction_ids:
                                            filtered_interactions[interaction_id] = interaction_data
                                    field_value = filtered_interactions
                                except json.JSONDecodeError:
                                    field_value = {}
                            elif isinstance(field_value, dict):
                                # Filter the dictionary directly
                                filtered_interactions = {}
                                for interaction_id, interaction_data in field_value.items():
                                    if interaction_id in chat_interaction_ids:
                                        filtered_interactions[interaction_id] = interaction_data
                                field_value = filtered_interactions
                            else:
                                field_value = {}
                        
                        properties[field_name] = field_value
                    
                    # Create GeoJSON feature
                    geojson_feature = {
                        "type": "Feature",
                        "geometry": geojson_geometry,
                        "properties": properties
                    }
                    
                    geojson_features.append(geojson_feature)
        
        # Create final GeoJSON
        geojson = {
            "type": "FeatureCollection",
            "features": geojson_features
        }

        # Save GeoJSON file
        geojson_path = os.path.join(export_folder_path, "chat_features.geojson")
        with open(geojson_path, 'w', encoding='utf-8') as geojson_file:
            json.dump(geojson, geojson_file, indent=2)

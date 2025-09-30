import os
import math
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
import requests
import litellm

from .settings import SettingsDialog
from .db import LogsDB
from .utils import raw_image_utils as ru
from .custom_qt import (zoom_to_and_flash_feature, CustomTextBrowser, ImageDisplayWidget,
                        AreaDrawingTool, IdentifyDrawnAreaTool)

from qgis.PyQt.QtGui import QPixmap, QImage, QColor, QTextOption, QPalette
from qgis.PyQt.QtCore import (QBuffer, QByteArray, Qt, QSettings, QVariant, QSize, QTimer, QSignalBlocker,
                              QObject, QThread, pyqtSignal)
from qgis.PyQt.QtWidgets import (QSizePolicy, QFileDialog, QMessageBox, QInputDialog, QComboBox, QLabel, QVBoxLayout,
                                 QPushButton, QWidget, QTextEdit, QApplication, QRadioButton, QHBoxLayout, QDockWidget,
                                 QSplitter, QListWidget, QListWidgetItem, QDialog, QTextBrowser, QCheckBox)
from qgis.core import (QgsVectorLayer, QgsRasterLayer, QgsSymbol, QgsSimpleLineSymbolLayer, QgsUnitTypes,
                       QgsRectangle, QgsWkbTypes, QgsProject, QgsGeometry, QgsMapRendererParallelJob, QgsFeature,
                       QgsField, QgsVectorFileWriter, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeatureRequest, QgsLayerTreeLayer)


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

    def run(self):
        response_text = ""
        reasoning_text = ""
        stream_success = False
        stream_error = None

        if self.stream_supported:
            try:
                response_stream = litellm.completion(
                    model=self.model,
                    messages=self.messages,
                    stream=True,
                    **self.base_kwargs
                )
                for chunk in response_stream:
                    text_chunk, reasoning_chunk = self.parse_stream_chunk(chunk)
                    if text_chunk:
                        response_text += text_chunk
                    if reasoning_chunk:
                        reasoning_text += reasoning_chunk
                    if text_chunk or reasoning_chunk:
                        self.chunk_received.emit(text_chunk, reasoning_chunk)
                stream_success = True
            except Exception as error:
                stream_error = error
                self.stream_failed.emit(error)

        if not stream_success:
            try:
                non_stream_response = litellm.completion(
                    model=self.model,
                    messages=self.messages,
                    **self.base_kwargs
                )
            except Exception as exc:
                error_payload = {"stream_error": stream_error, "final_error": exc}
                self.failed.emit(error_payload)
                self.done.emit()
                return

            response_text, reasoning_text = self.parse_completion_response(non_stream_response)

        self.completed.emit({
            "response_text": response_text or "",
            "reasoning_text": reasoning_text or "",
            "stream_success": stream_success,
            "stream_error": stream_error,
        })
        self.done.emit()


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
        self.send_to_mllm_button.setStyleSheet("background-color: #4CAF50; color: white; padding: 10px;")
        self.send_to_mllm_button.clicked.connect(self.send_to_mllm_fn)
        self.send_to_mllm_button.setToolTip("Send your prompt and selected image chips to the Multimodal Large Language Model")
        main_content_layout.addWidget(self.send_to_mllm_button)

        self.supported_api_clients = {
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

        self.added_models = self.load_added_models()
        self.available_api_clients = {}

        api_model_layout = QVBoxLayout()

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

        self.add_model_button = QPushButton("Add Model")
        self.add_model_button.setToolTip("Add a model for the selected MLLM service")
        self.add_model_button.clicked.connect(self.add_model)
        api_model_layout.addWidget(self.add_model_button)

        self.refresh_available_api_clients()

        main_content_layout.addLayout(api_model_layout, stretch=1)

        main_content_widget.setLayout(main_content_layout)
        splitter.addWidget(main_content_widget)

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

            if litellm.supports_reasoning(entry['mllm_model']):
                toggle_label = "Hide reasoning" if entry.get("reasoning_visible") else "Show reasoning"
                html_parts.append(
                    f'<div style="margin: 4px 0;"> {markdown.markdown(assistant_turn_start_text)}'
                    f'    <a href="toggle://reasoning/{entry["display_id"]}" style="text-decoration: none;">'
                    f'{toggle_label}'
                    f'    </a>'
                    f'</div>'
                )
                assistant_turn_start_text = ""
                if entry.get("reasoning_visible"):
                    html_parts.append(self._build_reasoning_html(entry))

            if response_text:
                assistant_markdown = markdown.markdown(f"{assistant_turn_start_text} {response_text}")
            else:
                assistant_markdown = markdown.markdown(assistant_turn_start_text)

            html_parts.append(f'<div>{assistant_markdown}</div>')

        html = ''.join(html_parts)
        self.chat_history.setHtml(html)

        if scroll_to_end:
            self.chat_history.verticalScrollBar().setValue(
                self.chat_history.verticalScrollBar().maximum()
            )

    @staticmethod
    def _build_reasoning_html(entry):
        reasoning_text = entry.get("reasoning_stream") if entry.get("is_pending") else entry.get("reasoning")
        reasoning_text = reasoning_text or ""

        if reasoning_text.strip():
            reasoning_html = markdown.markdown(reasoning_text)
        else:
            if entry.get("is_pending"):
                reasoning_html = "<p><i>Reasoning content not yet available.</i></p>"
            else:
                reasoning_html = "<p><i>Reasoning content not available.</i></p>"

        return (
            '<div class="reasoning-block" '
            'style="border-left: 2px solid #ccc; padding: 6px 8px; margin: 4px 0 12px; background-color: #fafafa;">'
            f'{reasoning_html}'
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
        """Populate the provider dropdown based on available environment variables."""
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
            self.add_model_button.setEnabled(False)
            self.api_selection.blockSignals(False)
            return

        self.api_selection.setEnabled(True)
        self.model_selection.setEnabled(True)
        self.add_model_button.setEnabled(True)
        for api_name in self.available_api_clients:
            self.api_selection.addItem(api_name)
        self.api_selection.blockSignals(False)
        self.update_model_choices()
        self.update_reasoning_controls_state()

    @staticmethod
    def get_missing_env_vars(api_config):
        missing = []
        for env_info in api_config.get("env_vars", []):
            if isinstance(env_info, dict):
                name = env_info.get("name")
                required = env_info.get("required", True)
            else:
                name = env_info
                required = True
            if not name:
                continue
            if required and not os.getenv(name):
                missing.append(name)
        return missing

    @staticmethod
    def build_auth_params(api_config):
        params = {}
        for env_info in api_config.get("env_vars", []):
            if not isinstance(env_info, dict):
                continue
            name = env_info.get("name")
            param = env_info.get("param")
            if not name or not param:
                continue
            value = os.getenv(name)
            if value:
                params[param] = value
        return params

    def add_model(self):
        if not self.available_api_clients:
            QMessageBox.information(self.iface.mainWindow(), "No Provider", "Configure an MLLM provider before adding models.")
            return
        api = self.api_selection.currentText()
        if api not in self.available_api_clients:
            QMessageBox.warning(self.iface.mainWindow(), "Error", "Select a configured MLLM provider first.")
            return
        model_name, ok = QInputDialog.getText(self.iface.mainWindow(), "Add Model", "Enter the full model identifier:")
        if not ok or not model_name.strip():
            return
        model_name = model_name.strip()
        existing = self._deduplicate_preserve_order(
            self.supported_api_clients.get(api, {}).get("models", []) +
            self.added_models.get(api, [])
        )
        if model_name in existing:
            QMessageBox.information(self.iface.mainWindow(), "Model Exists", "That model is already listed for this provider.")
            return
        if not litellm.supports_vision(model=model_name):
            QMessageBox.information(self.iface.mainWindow(), "Model Not Supported",
                                    f"""Model "{model_name}" does not exist or does not support vision. """
                                    f"""Please double-check spelling and vision capabilities.""")
            return
        self.added_models.setdefault(api, [])
        self.added_models[api].append(model_name)
        self.added_models[api] = self._deduplicate_preserve_order(self.added_models[api])
        self.save_added_models()
        self.update_model_choices(select_model=model_name)

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
        supported = litellm.supports_reasoning(model=self.model_selection.currentText())
        self.reasoning_effort_combo.setEnabled(supported)
        self.reasoning_effort_label.setEnabled(supported)

    def _handle_stream_failure(self, entry, error):
        QMessageBox.information(
            self.iface.mainWindow(),
            "Streaming Error",
            str(error)
        )
        entry["response_stream"] = ""
        entry["reasoning_stream"] = ""
        entry["has_reasoning"] = False
        self.render_chat_history()
        QApplication.processEvents()

    def build_reasoning_params(self, model_name):
        if not litellm.supports_reasoning(model=model_name):
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
            kwargs = dict(api_config.get("litellm_params", {}))
            kwargs.update(self.build_auth_params(api_config))
            needs_reasoning = litellm.supports_reasoning(model=preferred_model)
            return preferred_api, preferred_model, kwargs, needs_reasoning

        def build_base_kwargs(api_name):
            api_config = self.supported_api_clients.get(api_name, {})
            kwargs = dict(api_config.get("litellm_params", {}))
            kwargs.update(self.build_auth_params(api_config))
            return kwargs

        if preferred_api in available_clients:
            if not litellm.supports_reasoning(model=preferred_model):
                kwargs = build_base_kwargs(preferred_api)
                return preferred_api, preferred_model, kwargs, False

            for candidate in self._models_for_api(preferred_api):
                if not litellm.supports_reasoning(model=candidate):
                    kwargs = build_base_kwargs(preferred_api)
                    return preferred_api, candidate, kwargs, False

        for api_name in available_clients:
            if api_name == preferred_api:
                continue
            for candidate in self._models_for_api(api_name):
                if not litellm.supports_reasoning(model=candidate):
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

        needs_reasoning = litellm.supports_reasoning(model=fallback_model)
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
                "No configured MLLM service found. Set the required environment variables and restart QGIS."
            )
            return

        selected_model = self.model_selection.currentText()
        if not selected_model:
            QMessageBox.warning(self, "Error", "No model available for the selected MLLM service.")
            return

        api_config = self.supported_api_clients[selected_api]
        missing_env = self.get_missing_env_vars(api_config)
        if missing_env:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Error",
                f"{selected_api} configuration incomplete. Missing environment variables: {', '.join(missing_env)}."
            )
            return

        auth_params = self.build_auth_params(api_config)
        base_completion_kwargs = dict(api_config.get("litellm_params", {}))
        base_completion_kwargs.update(auth_params)
        reasoning_params = self.build_reasoning_params(selected_model)
        base_completion_kwargs.update(reasoning_params)

        prompt = self.prompt_input.toPlainText()
        if not prompt.strip():
            QMessageBox.warning(self, "Error", "Please enter a prompt.")
            return

        self.prompt_input.clear()

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

        worker.chunk_received.connect(
            lambda text, reasoning, eid=entry_id: self._handle_stream_chunk(eid, text, reasoning)
        )
        worker.stream_failed.connect(
            lambda error, eid=entry_id: self._on_stream_failed(eid, error)
        )
        worker.completed.connect(
            lambda payload, eid=entry_id: self._on_stream_completed(eid, payload)
        )
        worker.failed.connect(
            lambda payload, eid=entry_id: self._on_stream_error(eid, payload)
        )
        worker.done.connect(thread.quit)
        worker.done.connect(
            lambda eid=entry_id: self._on_stream_done(eid)
        )
        worker.done.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.started.connect(worker.run)
        thread.start()

    def _get_rendered_entry(self, entry_id):
        for entry in self.rendered_interactions:
            if entry.get("display_id") == entry_id:
                return entry
        return None

    def _handle_stream_chunk(self, entry_id, text_chunk, reasoning_chunk):
        entry = self._get_rendered_entry(entry_id)
        if not entry:
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

    def _on_stream_failed(self, entry_id, error):
        entry = self._get_rendered_entry(entry_id)
        if entry:
            self._handle_stream_failure(entry, error)

    def _on_stream_completed(self, entry_id, payload):
        entry = self._get_rendered_entry(entry_id)
        context = self.active_streams.get(entry_id)
        if not entry or context is None:
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
        if isinstance(error_payload, dict):
            final_error = error_payload.get("final_error")
        entry = self._get_rendered_entry(entry_id)

        if context:
            user_idx = context.get("user_message_index")
            if user_idx is not None and 0 <= user_idx < len(self.conversation):
                self.conversation = self.conversation[:user_idx]

        if entry and entry in self.rendered_interactions:
            self.rendered_interactions.remove(entry)

        self.render_chat_history()

        message = str(final_error) if final_error else "An error occurred while completing the request."
        QMessageBox.warning(self.iface.mainWindow(), "Error", message)
        self._cleanup_active_stream(entry_id, context)
        self.reload_current_chat()

    def _on_stream_done(self, entry_id):
        context = self.active_streams.get(entry_id)
        if not context:
            return
        context.pop("worker", None)
        context.pop("thread", None)

    def _finalize_interaction(self, entry_id, entry, context, response_text, reasoning_text):
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
                **summary_kwargs
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

        settings_dialog = SettingsDialog(self.iface.mainWindow())
        settings_dialog.sync_local_logs_dir_with_s3(self.logs_dir)

        self._cleanup_active_stream(entry_id, context)

        self.reload_current_chat()

    def _cleanup_active_stream(self, entry_id, context=None):
        ctx = context if context is not None else self.active_streams.get(entry_id)
        self.active_streams.pop(entry_id, None)
        if not ctx:
            return
        thread = ctx.get("thread")
        if thread and thread.isRunning():
            thread.quit()

    def reload_current_chat(self):
        item = self.chat_list.currentItem()
        self.chat_list.setCurrentItem(item)
        self.load_chat(item)
        self.chat_history.verticalScrollBar().setValue(self.chat_history.verticalScrollBar().maximum())
        
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

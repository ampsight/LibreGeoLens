import json
import sqlite3

from .utils.logger import get_logger


class LogsDB:
    # Current database schema version
    CURRENT_VERSION = 2
    
    def __init__(self, db_path):
        self.db_path = db_path
        self.logger = get_logger(__name__)

    def initialize_database(self):
        """Initialize the database or migrate it if necessary"""
        # Check if database exists and needs migration
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Check if SchemaVersion table exists
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='SchemaVersion'
            """)
            schema_version_exists = cursor.fetchone() is not None
            
            if not schema_version_exists:
                # New database or old version without versioning
                self._setup_new_database(conn, cursor)
            else:
                # Check current version and migrate if needed
                cursor.execute("SELECT version FROM SchemaVersion")
                current_version = cursor.fetchone()[0]
                
                if current_version < self.CURRENT_VERSION:
                    self._migrate_database(conn, cursor, current_version)
            
            conn.close()
            
        except sqlite3.Error as e:
            self.logger.error(f"Database initialization error: {e}")
            raise
    
    def _setup_new_database(self, conn, cursor):
        """Set up a new database or add versioning to existing one"""
        # Enable WAL mode for better performance with concurrent reads/writes
        conn.execute("PRAGMA journal_mode=WAL")
        # Ensure synchronous mode is set for best performance while maintaining integrity
        conn.execute("PRAGMA synchronous=NORMAL")
        
        # Check if tables already exist (old database without versioning)
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='Chips'
        """)
        tables_exist = cursor.fetchone() is not None
        
        if not tables_exist:
            # Create tables for a new database
            self._create_tables(conn, cursor)
        else:
            # Existing database without versioning - check structure and migrate if needed
            self.logger.info("Found existing database without versioning. Adding version tracking.")
            self._add_version_tracking(conn, cursor)
    
    def _create_tables(self, conn, cursor):
        """Create all database tables for a fresh installation"""
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Chips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_path TEXT NOT NULL,
                geocoords TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text_input TEXT NOT NULL,
                text_output TEXT NOT NULL,
                chips_sequence TEXT NOT NULL,
                mllm_service TEXT NOT NULL,
                mllm_model TEXT NOT NULL,
                chips_mode_sequence TEXT NOT NULL,
                chips_original_resolutions TEXT,
                chips_actual_resolutions TEXT,
                reasoning_output TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interactions_sequence TEXT NOT NULL,
                summary TEXT NOT NULL
            )
        """)
        
        # Create indexes to speed up common queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chip_id ON Chips(id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_id ON Chats(id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_interaction_id ON Interactions(id)")
        
        # Create version tracking table
        cursor.execute("""
            CREATE TABLE SchemaVersion (
                version INTEGER NOT NULL
            )
        """)
        
        # Insert current version
        cursor.execute("INSERT INTO SchemaVersion (version) VALUES (?)", (self.CURRENT_VERSION,))
        
        conn.commit()
        self.logger.info(f"Created new database with schema version {self.CURRENT_VERSION}")
    
    def _add_version_tracking(self, conn, cursor):
        """Add version tracking to an existing database"""
        # Check the structure to determine version
        try:
            # Check if Interactions table has chips_original_resolutions and chips_actual_resolutions
            cursor.execute("PRAGMA table_info(Interactions)")
            columns = {column[1] for column in cursor.fetchall()}
            
            # Create version tracking table
            cursor.execute("""
                CREATE TABLE SchemaVersion (
                    version INTEGER NOT NULL
                )
            """)
            
            # Determine version based on structure
            if 'chips_original_resolutions' in columns and 'chips_actual_resolutions' in columns:
                cursor.execute("INSERT INTO SchemaVersion (version) VALUES (?)", (self.CURRENT_VERSION,))
                self.logger.info(f"Added version tracking to existing database - current version: {self.CURRENT_VERSION}")
            else:
                # Old structure, needs migration
                cursor.execute("INSERT INTO SchemaVersion (version) VALUES (?)", (0,))
                self.logger.info("Added version tracking to existing database - needs migration from version 0")
                # Perform migration to latest version
                self._migrate_database(conn, cursor, 0)
            
            conn.commit()
        except sqlite3.Error as e:
            self.logger.error(f"Error adding version tracking: {e}")
            conn.rollback()
            raise
    
    def _migrate_database(self, conn, cursor, from_version):
        """Migrate database from specified version to current version"""
        self.logger.info(f"Migrating database from version {from_version} to {self.CURRENT_VERSION}")
        
        try:
            # Apply all necessary migrations in sequence
            if from_version < 1:
                self._migrate_to_v1(conn, cursor)
            if from_version < 2:
                self._migrate_to_v2(conn, cursor)
            
            # Update schema version
            cursor.execute("UPDATE SchemaVersion SET version = ?", (self.CURRENT_VERSION,))
            conn.commit()
            self.logger.info(f"Database successfully migrated to version {self.CURRENT_VERSION}")
        except sqlite3.Error as e:
            self.logger.error(f"Migration error: {e}")
            conn.rollback()
            raise
    
    def _migrate_to_v1(self, conn, cursor):
        """Migrate database to version 1"""
        self.logger.info("Applying migration to version 1")
        
        # Check if we need to add the chips_original_resolutions and chips_actual_resolutions columns
        cursor.execute("PRAGMA table_info(Interactions)")
        columns = {column[1] for column in cursor.fetchall()}
        
        if 'chips_original_resolutions' not in columns:
            cursor.execute("ALTER TABLE Interactions ADD COLUMN chips_original_resolutions TEXT")
            self.logger.info("Added chips_original_resolutions column to Interactions table")
        
        if 'chips_actual_resolutions' not in columns:
            cursor.execute("ALTER TABLE Interactions ADD COLUMN chips_actual_resolutions TEXT")
            self.logger.info("Added chips_actual_resolutions column to Interactions table")

        conn.commit()

    def _migrate_to_v2(self, conn, cursor):
        """Migrate database to version 2"""
        self.logger.info("Applying migration to version 2")

        cursor.execute("PRAGMA table_info(Interactions)")
        columns = {column[1] for column in cursor.fetchall()}

        if 'reasoning_output' not in columns:
            cursor.execute("ALTER TABLE Interactions ADD COLUMN reasoning_output TEXT")
            self.logger.info("Added reasoning_output column to Interactions table")

        conn.commit()

    def save_chip(self, image_path, geocoords):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO Chips (image_path, geocoords)
            VALUES (?, ?)
        """, (image_path, str(geocoords)))

        chip_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return chip_id

    def save_interaction(self, text_input, text_output, chips_sequence, mllm_service, mllm_model,
                         chips_mode_sequence, chips_original_resolutions=None, chips_actual_resolutions=None,
                         reasoning_output=None):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO Interactions (text_input, text_output, chips_sequence, mllm_service, mllm_model,
                                     chips_mode_sequence, chips_original_resolutions, chips_actual_resolutions,
                                     reasoning_output)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (text_input, text_output, str(chips_sequence), mllm_service, mllm_model, 
              str(chips_mode_sequence), str(chips_original_resolutions), str(chips_actual_resolutions),
              reasoning_output))

        interaction_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return interaction_id

    def save_chat(self, interactions_sequence):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO Chats (interactions_sequence, summary)
            VALUES (?, ?)
        """, (str(interactions_sequence), "",))

        chat_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return chat_id

    def fetch_all_chips(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM Chips")
        chips = cursor.fetchall()
        conn.close()
        return chips

    def fetch_chip_by_id(self, chip_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM Chips WHERE id = ?", (chip_id,))
        chip = cursor.fetchone()
        conn.close()
        return chip

    def fetch_all_interactions(self):
        conn = sqlite3.connect(self.db_path)
        # Enable WAL mode for better performance with concurrent reads/writes
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM Interactions")
        interactions = cursor.fetchall()
        conn.close()
        return interactions

    def fetch_interaction_by_id(self, interaction_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM Interactions WHERE id = ?", (interaction_id,))
        interaction = cursor.fetchone()
        conn.close()
        return interaction

    def fetch_all_chats(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM Chats")
        chats = cursor.fetchall()
        conn.close()
        return chats

    def fetch_chat_by_id(self, chat_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM Chats WHERE id = ?", (chat_id,))
        chat = cursor.fetchone()
        conn.close()
        return chat

    def add_new_interaction_to_chat(self, chat_id, interaction_id):
        chats = self.fetch_all_chats()
        selected_chat = next(chat for chat in chats if chat[0] == chat_id)
        interactions_sequence = json.loads(selected_chat[1])

        # Append the new interaction ID
        interactions_sequence.append(interaction_id)

        # Update the chat in the database
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE Chats SET interactions_sequence = ? WHERE id = ?",
            (json.dumps(interactions_sequence), chat_id),
        )
        conn.commit()
        conn.close()

    def update_chat_summary(self, chat_id, summary):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute(
            "UPDATE Chats SET summary = ? WHERE id = ?",
            (summary, chat_id),
        )
        conn.commit()
        conn.close()

    def update_chip_image_path(self, chip_id, image_path):
        """Update the image path for a chip"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE Chips SET image_path = ? WHERE id = ?",
            (image_path, chip_id)
        )
        conn.commit()
        conn.close()


    def delete_chat(self, chat_id, delete_chips):
        """Delete a chat and its associated data"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get chat's interactions
        cursor.execute("SELECT interactions_sequence FROM Chats WHERE id = ?", (chat_id,))
        interactions_sequence = json.loads(cursor.fetchone()[0])

        if delete_chips:
            # Get chips associated with these interactions
            chips_to_check = set()
            for interaction_id in interactions_sequence:
                cursor.execute("SELECT chips_sequence FROM Interactions WHERE id = ?", (interaction_id,))
                chips_sequence = json.loads(cursor.fetchone()[0])
                chips_to_check.update(chips_sequence)

            # For each chip, check if it's used in other chats' interactions
            chips_to_delete = set()
            for chip_id in chips_to_check:
                is_used = False
                # Get all interactions from other chats
                cursor.execute("SELECT interactions_sequence FROM Chats WHERE id != ?", (chat_id,))
                other_chats_interactions = []
                for chat in cursor.fetchall():
                    other_chats_interactions.extend(json.loads(chat[0]))

                # Get chips from those interactions
                for interaction_id in other_chats_interactions:
                    cursor.execute("SELECT chips_sequence FROM Interactions WHERE id = ?", (interaction_id,))
                    other_interaction = cursor.fetchone()
                    other_chips = json.loads(other_interaction[0])
                    if chip_id in other_chips:
                        is_used = True
                        break
                if not is_used:
                    chips_to_delete.add(chip_id)
                    cursor.execute("SELECT image_path FROM Chips WHERE id = ?", (chip_id,))
                    image_path = cursor.fetchone()[0]
                    # Return image paths for deletion
                    yield image_path, chip_id
        else:
            chips_to_delete = []

        # Delete interactions
        for interaction_id in interactions_sequence:
            cursor.execute("DELETE FROM Interactions WHERE id = ?", (interaction_id,))

        # Delete chips
        for chip_id in chips_to_delete:
            cursor.execute("DELETE FROM Chips WHERE id = ?", (chip_id,))

        # Delete chat
        cursor.execute("DELETE FROM Chats WHERE id = ?", (chat_id,))

        conn.commit()
        conn.close()

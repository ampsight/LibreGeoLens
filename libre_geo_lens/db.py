import json
import sqlite3


class LogsDB:
    def __init__(self, db_path):
        self.db_path = db_path

    def initialize_database(self):
        conn = sqlite3.connect(self.db_path)
        # Enable WAL mode for better performance with concurrent reads/writes
        conn.execute("PRAGMA journal_mode=WAL")
        # Ensure synchronous mode is set for best performance while maintaining integrity
        conn.execute("PRAGMA synchronous=NORMAL")
        
        cursor = conn.cursor()

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
                chips_mode_sequence TEXT NOT NULL
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

        conn.commit()
        conn.close()

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

    def save_interaction(self, text_input, text_output, chips_sequence, mllm_service, mllm_model, chips_mode_sequence):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO Interactions (text_input, text_output, chips_sequence, mllm_service, mllm_model, chips_mode_sequence)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (text_input, text_output, str(chips_sequence), mllm_service, mllm_model, str(chips_mode_sequence)))

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


    def delete_chat(self, chat_id):
        """Delete a chat and its associated data"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get chat's interactions
        cursor.execute("SELECT interactions_sequence FROM Chats WHERE id = ?", (chat_id,))
        interactions_sequence = json.loads(cursor.fetchone()[0])

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

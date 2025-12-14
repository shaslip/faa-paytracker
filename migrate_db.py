import sqlite3
import pandas as pd
import os

DB_FILE = "paystubs.db"

def migrate():
    if not os.path.exists(DB_FILE):
        print("Database not found!")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Check if we already migrated
    try:
        cursor.execute("SELECT year FROM user_schedule LIMIT 1")
        print("Migration already applied.")
        conn.close()
        return
    except sqlite3.OperationalError:
        print("Migrating database to support multiple years...")

    # 1. Read existing data
    try:
        old_df = pd.read_sql("SELECT * FROM user_schedule", conn)
        print(f"Found {len(old_df)} rows in existing schedule.")
    except:
        old_df = pd.DataFrame()

    # 2. Rename old table to backup
    cursor.execute("ALTER TABLE user_schedule RENAME TO user_schedule_old")

    # 3. Create new table with Composite Primary Key (year + day_of_week)
    cursor.execute("""
        CREATE TABLE user_schedule (
            year INTEGER NOT NULL,
            day_of_week INTEGER NOT NULL,
            start_time TEXT,
            end_time TEXT,
            is_workday BOOLEAN,
            PRIMARY KEY (year, day_of_week)
        )
    """)

    # 4. Insert old data labeled as 2025
    if not old_df.empty:
        old_df['year'] = 2025
        # Ensure column order matches insert
        old_df = old_df[['year', 'day_of_week', 'start_time', 'end_time', 'is_workday']]
        old_df.to_sql('user_schedule', conn, if_exists='append', index=False)
        print("Existing schedule migrated to Year 2025.")
    
    # 5. Seed empty 2024 and 2026 for convenience (Optional)
    # We leave them blank so the logic returns 00:00 for them until edited.
    
    conn.commit()
    conn.close()
    print("Migration complete!")

if __name__ == "__main__":
    migrate()

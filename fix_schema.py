import sqlite3
import pandas as pd
import os

# Ensure this matches the DB name in your models.py
DB_NAME = 'payroll_audit.db'

def force_migration():
    print(f"🔍 Looking for {DB_NAME} in current directory...")
    
    # Check if DB exists
    if not os.path.exists(DB_NAME):
        print(f"❌ ERROR: {DB_NAME} not found.")
        print(f"   Current Directory: {os.getcwd()}")
        print("   Please ensure you run this script from the 'PayTracker' folder.")
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    print("🔌 Connected to database.")

    # Check columns in the current table
    try:
        cursor.execute("PRAGMA table_info(user_schedule)")
        columns = [row[1] for row in cursor.fetchall()]
        print(f"   Current columns: {columns}")

        if 'year' in columns:
            print("✅ The 'year' column already exists. No action needed.")
            conn.close()
            return
    except Exception as e:
        print(f"   Error checking columns: {e}")

    print("⚠️ 'year' column missing. Starting forced migration...")

    # 1. Backup old data
    try:
        old_df = pd.read_sql("SELECT * FROM user_schedule", conn)
        print(f"   Backed up {len(old_df)} rows of schedule data.")
    except Exception as e:
        print(f"   Warning: Could not read old data ({e}). Starting fresh.")
        old_df = pd.DataFrame()

    # 2. Rename the old table out of the way
    try:
        cursor.execute("DROP TABLE IF EXISTS user_schedule_backup")
        cursor.execute("ALTER TABLE user_schedule RENAME TO user_schedule_backup")
        print("   Old table renamed to 'user_schedule_backup'.")
    except Exception as e:
        print(f"❌ Error renaming table: {e}")
        conn.close()
        return

    # 3. Create the NEW table with the 'year' column
    try:
        cursor.execute('''
            CREATE TABLE user_schedule (
                year INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                start_time TEXT,
                end_time TEXT,
                is_workday BOOLEAN,
                PRIMARY KEY (year, day_of_week)
            )
        ''')
        print("   New 'user_schedule' table created successfully.")
    except Exception as e:
        print(f"❌ Error creating new table: {e}")
        conn.close()
        return

    # 4. Restore the data (Defaulting old data to 2025)
    if not old_df.empty:
        old_df['year'] = 2025
        # Select only the columns that match the new table
        old_df = old_df[['year', 'day_of_week', 'start_time', 'end_time', 'is_workday']]
        old_df.to_sql('user_schedule', conn, if_exists='append', index=False)
        print("   Restored existing schedule as Year 2025.")

    conn.commit()
    conn.close()
    print("✅ MIGRATION SUCCESS! You can now run the dashboard.")

if __name__ == "__main__":
    force_migration()

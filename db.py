import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "profiles.db")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            '''CREATE TABLE IF NOT EXISTS profiles(
                   id TEXT PRIMARY KEY,
                   name TEXT UNIQUE NOT NULL,
                   proxy TEXT DEFAULT '',
                   state TEXT DEFAULT 'stopped',
                   pid INTEGER DEFAULT NULL,
                   last_url TEXT DEFAULT ''
               )'''
        )
        conn.commit()

@contextmanager
def db() :
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

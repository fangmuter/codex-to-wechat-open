import sqlite3


class SessionMetaStore:
    """Non-sensitive planner-to-fusion metadata and canonical snapshots."""
    _EXTRA_COLUMNS = {
        "fused_json": "TEXT",
        "fusion_hash": "TEXT",
        "confirmed_fused_json": "TEXT",
        "confirmed_at": "TEXT",
    }

    def __init__(self, path=':memory:'):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS session_meta(
          planner_session_id INTEGER PRIMARY KEY,target_date TEXT NOT NULL,
          planning_window TEXT,available_minutes INTEGER NOT NULL,energy TEXT NOT NULL,
          user_arranged_json TEXT NOT NULL,revision TEXT NOT NULL,
          formatted_text TEXT NOT NULL,fused_json TEXT,fusion_hash TEXT,
          confirmed_fused_json TEXT,confirmed_at TEXT)''')
        existing = {row['name'] for row in self.db.execute('PRAGMA table_info(session_meta)')}
        for name, kind in self._EXTRA_COLUMNS.items():
            if name not in existing:
                self.db.execute(f'ALTER TABLE session_meta ADD COLUMN {name} {kind}')
        self.db.commit()

    def close(self):
        self.db.close()

    def put(self, planner_session_id, target_date, planning_window, available_minutes,
            energy, user_arranged_json, revision, formatted_text,
            fused_json=None, fusion_hash=None):
        self.db.execute('''INSERT OR REPLACE INTO session_meta(
          planner_session_id,target_date,planning_window,available_minutes,energy,
          user_arranged_json,revision,formatted_text,fused_json,fusion_hash,
          confirmed_fused_json,confirmed_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)''',
          (planner_session_id, target_date, planning_window, available_minutes,
           energy, user_arranged_json, revision, formatted_text, fused_json,
           fusion_hash))
        self.db.commit()

    def mark_confirmed(self, planner_session_id, fused_json, confirmed_at):
        self.db.execute('''UPDATE session_meta SET confirmed_fused_json=?,confirmed_at=?
                           WHERE planner_session_id=?''',
                        (fused_json, confirmed_at, planner_session_id))
        self.db.commit()

    def get(self, planner_session_id):
        return self.db.execute('SELECT * FROM session_meta WHERE planner_session_id=?',
                               (planner_session_id,)).fetchone()

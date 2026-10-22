"""Optional local history persistence for recent-domain anti-repetition."""
import sqlite3


class RecommendationHistory:
    def __init__(self,path=":memory:"):
        self.db=sqlite3.connect(path); self.db.execute("CREATE TABLE IF NOT EXISTS recommendation_history(target_date TEXT NOT NULL, domain TEXT NOT NULL, title TEXT NOT NULL, PRIMARY KEY(target_date,domain,title))"); self.db.commit()
    def close(self): self.db.close()
    def record(self,target_date,recommendations):
        self.db.executemany("INSERT OR IGNORE INTO recommendation_history VALUES(?,?,?)",[(target_date,x.domain,x.title) for x in recommendations]); self.db.commit()
    def recent_domains(self,limit_days=2):
        rows=self.db.execute("SELECT domain FROM recommendation_history ORDER BY target_date DESC LIMIT ?",(limit_days*3,)).fetchall()
        return tuple(row[0] for row in rows)

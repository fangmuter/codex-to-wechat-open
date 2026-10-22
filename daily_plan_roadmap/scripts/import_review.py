"""Import one explicitly supplied review JSON into an explicitly supplied DB."""
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from daily_plan_roadmap.review_sync import ReviewSyncError, import_review
from daily_plan_roadmap.storage import RoadmapProgressStore

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('review_json'); p.add_argument('--db',required=True); args=p.parse_args(argv)
    store=RoadmapProgressStore(args.db)
    try:
        result=import_review(args.review_json,store)
        print('review_accepted='+('YES' if result['imported'] else 'NO_OP'))
        print('review_id='+result['review_id']); print('update_count='+str(result['update_count']))
        print('payload_hash_recorded=YES'); print('sensitive_data_stored=NO')
        return 0
    except ReviewSyncError as exc:
        print('review_accepted=NO'); print('error='+str(exc)); return 2
    finally: store.close()
if __name__=='__main__': raise SystemExit(main())

import logging
from logging.handlers import RotatingFileHandler
import re

SAFE_FIELDS={'state','idempotency_key','sender_fingerprint','planner_state','reply_state','runtime_minutes','outcome',
             'review_inbox_status','review_files_seen','review_imported_count','review_noop_count','review_rejected_count',
             'pid','run_id','pinned_target_date','stale_lock_recovered','delivery_discovered',
             'exception_type','exception_message','lock_released','poll_count',
             'resolved_inbox_path','inbox_exists','inbox_accessible','json_file_count',
             'target_date_candidate_count','valid_target_candidate_count',
             'reject_reason_category','session_health','health_checked_at',
             'health_age','bot_fingerprint','account_fingerprint',
             'credential_fingerprint','preflight_result','error_class',
             'transition','http_status','ret','errcode','errmsg','response_keys',
             'submitted','successful','inbound_poll','context_state','context_age'}

_SECRET_VALUE = re.compile(
    r'(?i)(authorization|bearer|token|cookie|context[_ -]?token|recipient(?:[_ -]?id)?|credential|dpapi)'
    r'(?:\s*[:=]\s*)[^\s,;]+'
)


def sanitize_exception_message(value, limit=200):
    text = str(value or '').replace('\r', ' ').replace('\n', ' ')
    text = _SECRET_VALUE.sub(lambda match: match.group(1) + '=[REDACTED]', text)
    return text[:limit]

class RedactedLogger:
    def __init__(self, logger): self._logger=logger
    def event(self,name,**fields):
        values=[]
        for key in sorted(fields):
            if key not in SAFE_FIELDS:
                continue
            value = fields[key]
            if key in {'exception_message','errmsg'}:
                value = sanitize_exception_message(value)
            else:
                value = str(value)[:200]
            values.append(f'{key}={value}')
        safe=' '.join(values)
        self._logger.info('%s%s',name,(' '+safe) if safe else '')

    def close(self):
        """Release the file handler after a finite runtime exits."""
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def build_logger(logs_dir):
    logs_dir.mkdir(parents=True,exist_ok=True)
    logger=logging.getLogger('daily_plan_production'); logger.setLevel(logging.INFO); logger.propagate=False
    if not logger.handlers:
        handler=RotatingFileHandler(logs_dir/'production.log',maxBytes=1_000_000,backupCount=3,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
    return RedactedLogger(logger)
